---
type: Architecture Decision
title: "ADR-0020: Native mode in the setup file"
description: target.mode native in nexo.yaml runs Nexo against this machine's own Neo4j and Ollama with no containers; nexo setup renders nexo.env alone, secrets stay in the provider, and a stale render is detected instead of applied silently.
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
- **Native renders `nexo.env` alone**: `NEXO_SETUP_MODE=native`, `NEO4J_URI` on `localhost`,
  `OTEL_EXPORTER_OTLP_ENDPOINT` blank (so one left in `.env` does not apply) and `OTEL_LOGS_EXPORTER=none`,
  plus the usual `NEXO_SECRETS_*`, so the password is read from the one store it lives in
  ([ADR-0017](adr-0017-secrets-from-a-provider.md)). No compose or collector file is written.
  Add-ons, patches and vLLM need containers and are rejected in native mode.[^render][^adr-0017]
- **A render matches its mode**: each render removes the files the previous render wrote and this
  one does not, so switching to native leaves no `compose.yaml` behind.
- **A stale render is detected**: `nexo.env` records the sha256 of the `nexo.yaml` it came from, and
  every `nexo` command except `nexo setup` warns when the file has changed since.[^config]
- **`up` and `check` in native mode** start the models and log in to the machine's Neo4j with the
  stored password; a failure says how to start Neo4j or give it that password
  (`nexo setup neo4j-init` runs `neo4j-admin dbms set-initial-password` for a Neo4j that has never
  started). `down` and `compose` refuse, as there is nothing to stop.
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

# Consequences

Native and Podman setups are both described by `nexo.yaml` and run with `make env`, `make up` and
`make check`. Telemetry export is off in native mode; running the collector by hand means setting
`OTEL_EXPORTER_OTLP_ENDPOINT` in the environment, which wins over the rendered env. A render from
before this change has no source line, so the first `nexo` command after upgrading asks for
`make render`.

[^schema]: Setup file schema
[^render]: nexo setup renderer
[^config]: Settings, render_drift and explain
[^adr-0016]: "ADR-0016: A declarative setup file"
[^adr-0017]: "ADR-0017: Secrets from a provider, not a file you copy"
