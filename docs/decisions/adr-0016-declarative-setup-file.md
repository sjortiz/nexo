---
type: Architecture Decision
title: "ADR-0016: A declarative setup file"
description: One nexo.yaml per machine, resolved into a lock and rendered by nexo setup into the compose stack; make keeps its targets and add-on flags; docker-compose.yml becomes a generated file.
tags: [adr, operations, setup, compose]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T16:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: setup-cli
    resource: ../../src/nexo/setup/commands.py
    title: nexo setup
  - id: local-setup
    resource: ../operations/local-setup.md
    title: How local setup works
  - id: foundry
    resource: https://github.com/SigNoz/foundry
    title: SigNoz foundry
---

# Context

Each add-on cost a Makefile flag, a collector fragment, a compose profile and documentation. The
collector loaded four `--config` files (a disabled add-on loaded an empty one), the GPU services lived
in a separate compose file only because Compose checks required variables in every loaded service,
and Linux needed its own override for host networking. The five setups (one Mac, two machines, Linux
CPU, Linux GPU, vLLM) were spread across Make, Compose and `.env`.

# Decision

- **One setup file, foundry-style.** `nexo.yaml` declares the target, the model servers, core
  components, opt-in add-ons and RFC 6902 patches, like SigNoz foundry's casting.[^foundry]
- **`nexo setup` in the Nexo package** checks the machine, resolves the file into `nexo.lock` (images
  per service, models per role; pins move only with `--upgrade`) and renders `.nexo/render/`:
  compose with only the enabled services, one collector config, and `nexo.env`, which Nexo reads
  after `.env`.[^setup-cli] Secrets stay in `.env`, generated only for enabled components.
- **Make stays the front door.** `make env`, `up`, `down`, `check` and `app` call `nexo setup`;
  `LANGFUSE=1`, `JAEGER=1` and `LANGSMITH=1` enable an add-on for one run. `make up` also runs
  `ollama-setup.sh` for this machine's roles.
- **`docker-compose.yml` is generated** from the `mac-single` preset with repo-relative paths (`make
  compose-file`), so plain compose and readers on GitHub still have it; a test fails when it is
  stale. The GPU and Linux overrides are removed.
- **Compose first.** Kubernetes comes after the compose target ships.[^local-setup]

# Options considered

| Option | Against |
|---|---|
| Use foundry itself | Its kinds model SigNoz only (no Neo4j, Langfuse or Ollama); a Go binary; usage reporting on by default. Kept as a reference when SigNoz is upgraded. |
| A separate tool | A second toolchain, and a copy of the profile catalog. |
| Keep compose files, add a selector | Keeps the multi-config collector, the override files and the always-loaded services. |
| Delete docker-compose.yml | Plain `compose up` from a clone would need `nexo setup`; the user chose a generated copy. |

# Consequences

Contributors edit fragments in `deploy/compose/` and `deploy/otel-addons/`, then run `make
compose-file`. Compose projects are named after `metadata.name` (default `nexo`), so volumes
created by the old file under the directory's name are not reused. Swarm, systemd, Helm and the
cloud targets foundry offers are not planned.

[^foundry]: SigNoz foundry
[^setup-cli]: nexo setup
[^local-setup]: How local setup works
