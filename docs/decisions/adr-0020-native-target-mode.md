---
type: Architecture Decision
title: "ADR-0020: Native mode in the setup file"
description: target.mode native in nexo.yaml keeps the core (Nexo, Neo4j, the models) on this machine and runs only telemetry and add-ons as containers, optionally in a named Podman machine; secrets stay in the provider, and a stale render is detected instead of applied silently.
tags: [adr, operations, setup, local]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T21:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: schema
    resource: ../../src/nexo/setup/schema.py
    title: Setup file schema
  - id: render
    resource: ../../src/nexo/setup/render.py
    title: nexo setup renderer
  - id: engine
    resource: ../../src/nexo/setup/engine.py
    title: Podman machines
  - id: config
    resource: ../../src/nexo/config.py
    title: Settings, render_drift and explain
  - id: adr-0016
    resource: adr-0016-declarative-setup-file.md
    title: "ADR-0016: A declarative setup file"
  - id: adr-0017
    resource: adr-0017-secrets-from-a-provider.md
    title: "ADR-0017: Secrets from a provider, not a file you copy"
---

# Context

The native path (Homebrew Neo4j, Ollama, Nexo from the `.venv`) lived outside `nexo.yaml`. Setting it
up on an M1 Max showed two problems with one cause:

- A `.nexo/render/nexo.env` left by an earlier `make env` or `make up` kept applying. Settings read it
  after `.env`, so a native run sent telemetry to `localhost:4318`, where no collector ran, with no
  warning.
- Without the rendered env, Settings did not consult the secrets provider, so the generated
  `NEO4J_PASSWORD` (already in `~/.config/nexo/config`) also had to be copied into `.env`.

Reading the default secrets file whenever no provider is configured would fix the second problem
alone, but it means an implicit read of a hidden file that nothing in the checkout names. The user
rejected that option.

# Decision

- **The native path is a mode of the setup file**: `spec.target.mode: native` (beside `compose`), with
  `mac-native` and `linux-native` presets. `nexo setup` stays the single entry point
  ([ADR-0016](adr-0016-declarative-setup-file.md)), and `.env` keeps personal settings only.[^schema][^adr-0016]
- **Native means a bare-metal core**: Nexo, Neo4j and the models run on this machine; containers are
  for telemetry and add-ons only. `nexo.env` sets `NEXO_SETUP_MODE=native`, `NEO4J_URI` on `localhost`,
  `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` (the collector's published loopback port) and
  the usual `NEXO_SECRETS_*`, so the password is read from the one store it lives in
  ([ADR-0017](adr-0017-secrets-from-a-provider.md)).[^render][^adr-0017]
- **Containers in native mode**: `compose.yaml` holds the content-stripping collector and SigNoz,
  plus the add-ons the setup turns on (never a `neo4j` or `nexo` service). Add-ons are reached on
  `localhost` ports, as `LANGFUSE_HOST` shows. Patches apply to those services only; one that
  touches `neo4j` or `nexo` is rejected. vLLM is rejected: it needs NVIDIA containers.
- **Telemetry is on by default and `spec.telemetry.enabled: false` turns it off** (native only;
  compose runs the collector as a core component). Off renders a blank endpoint and no collector or
  SigNoz; with nothing else in containers, no `compose.yaml` either. Add-ons that send through the
  collector (Langfuse, Jaeger, LangSmith, host metrics) need telemetry on; Agent Inbox does not.
- **A render matches its mode**: each render removes the files the previous render wrote and this
  one does not, so switching to native leaves no `compose.yaml` behind.
- **A stale render is detected**: `nexo.env` records the sha256 of the `nexo.yaml` it came from, and
  every `nexo` command except `nexo setup` warns when the file has changed since.[^config]
- **`up` and `check` in native mode** start the models, log in to the machine's Neo4j with the stored
  password (a failure says how to start it or give it that password; `nexo setup neo4j-init` runs
  `neo4j-admin dbms set-initial-password` for a Neo4j that has never started) and run `compose up`
  for the container services. `down` stops those containers only. `check` looks for an engine only
  when containers are on, and warns instead of failing when there is none.
- **A Podman machine per setup**: `spec.target.machine` names the machine whose connection `up` and
  `check` use. Podman 5.6 runs one machine at a time and stopping one stops every container on it,
  so `up` fails when another machine is active unless `--switch-machine` (`make up SWITCH=1`) is
  given, which stops it and starts the named one. `check` reports the active machine against the
  configured one and warns when the machine's memory is far above what the containers need, since
  that memory is not available to the models.[^engine]
- **Rootless is detected, not rendered around**: the host-metrics add-on mounts
  `/run/podman/podman.sock`, which only a rootful machine has (on a rootless one `up` fails with
  `mkdir /run/podman/podman.sock: permission denied`). `check` fails and prints
  `podman machine stop <m> && podman machine set --rootful <m> && podman machine start <m>`. Rendering
  without the `docker_stats` receiver was rejected: it would silently drop per-container metrics, and
  the rootless socket path depends on the user id inside the VM.
- **`nexo config --explain`** prints each effective setting with its source (environment, secrets
  store, rendered env, `.env`, derived from a profile, default), secret values redacted: the general
  answer to "where did this value come from?".

# Options considered

| Option | Against |
|---|---|
| Fall back to `~/.config/nexo/config` when no provider is configured | An implicit read of a hidden file; rejected by the user. |
| `nexo setup clean` to delete the render before a native run | A manual step that is easy to forget; the mismatch stays possible. |
| A per-service `neo4j: { provider: external, uri: ... }` | Finer than needed today: native means every service is external; `spec.ports.neo4jBolt` already sets the port. |
| Probe the OTLP endpoint and warn when nothing listens | Only covers telemetry, and adds a network probe to every command. |

# Amendment

The first version of this ADR rejected add-ons, patches and vLLM in native mode and rendered no
telemetry, a rule left over from a per-component runtime design that was dropped. It made a real
setup impossible to write: Nexo, Neo4j and Ollama on the host, with the collector and SigNoz in a
small Podman machine. Native now means only "bare metal for the core", and the decisions above
describe the current rule.

# Consequences

Native and Podman setups are both described by `nexo.yaml` and run with `make env`, `make up` and
`make check`. Telemetry is on in native mode through the container engine, or off with
`telemetry.enabled: false`; a collector run by hand needs `OTEL_EXPORTER_OTLP_ENDPOINT` in the
environment, which wins over the rendered env. A render from before this change has no source line,
so the first `nexo` command after upgrading asks for `make render`.

[^schema]: Setup file schema
[^render]: nexo setup renderer
[^engine]: Podman machines
[^config]: Settings, render_drift and explain
[^adr-0016]: "ADR-0016: A declarative setup file"
[^adr-0017]: "ADR-0017: Secrets from a provider, not a file you copy"
