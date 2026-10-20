---
type: Deployment Model
title: How local setup works
description: The mechanism behind local runs - three independent choices, five model roles, one profile catalog read by both the setup script and Nexo, who listens where, and how to check it.
resource: ../../src/nexo/local_profiles.py
tags: [operations, local, ollama, profiles, podman]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T23:40:00Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profile catalog
  - id: config
    resource: ../../src/nexo/config.py
    title: Settings and profile resolution
  - id: setup
    resource: ../../deploy/local/ollama-setup.sh
    title: Ollama setup script
  - id: compose
    resource: ../../docker-compose.yml
    title: Docker Compose stack
  - id: override
    resource: ../../docker-compose.linux.yml
    title: Linux compose override
  - id: models
    resource: ../../src/nexo/llm.py
    title: Model resolution and server check (nexo models)
---

# Three independent choices

| Choice | Options | Decided by |
|---|---|---|
| Which models | A hardware profile per machine: `m1-max-64`, `m5-pro-24`, `gpu-24`, `cpu-64` | `LOCAL_PROFILE` |
| How many machines | One, or two (the second serves the heavy models) | `REMOTE_PROFILE` set or not |
| How services run (Neo4j, telemetry, Nexo) | Podman containers or native installs | The commands you run; `.env` stays the same |

Models always run natively in Ollama: containers on macOS cannot use the GPU. Step-by-step:
[Local on Apple Silicon](local-apple-silicon.md), [Local on Linux](local-linux.md).

# Five model roles

| Role | Used for | One machine | Two machines |
|---|---|---|---|
| agent | The investigator's multi-turn tool calls | this machine | remote |
| extraction | Triples and entity resolution during ingest | this machine | remote |
| router | Intake validation | this machine | this machine |
| judge | Generation scores in `nexo eval`; off where the tier has no room | this machine | this machine |
| embedding | Chunk and query vectors | this machine | this machine |

# One catalog, two readers

```
                    src/nexo/local_profiles.py
              profile -> memory tier -> models per role set
                 │                                   │
   ollama-setup.sh PROFILE [ROLE_SET]          Nexo settings (.env)
   on each model machine:                      on the machine running Nexo:
   - pulls and preloads the models             - model and server URL per role
   - sizes Ollama: context length,             - reasoning off for JSON roles on thinking models
     parallelism, keep-alive, KV cache         - longer timeouts, extraction concurrency
```

Both read the same catalog,[^profiles] so they agree as long as the role sets match:

| Layout | On the model machine(s) | `.env` where Nexo runs | Role sets |
|---|---|---|---|
| One machine | `ollama-setup.sh m1-max-64` | `LOCAL_PROFILE=m1-max-64` | `single` |
| Two machines | remote: `ollama-setup.sh m1-max-64 heavy`; here: `ollama-setup.sh` | `LOCAL_PROFILE=m5-pro-24`, `REMOTE_PROFILE=m1-max-64` | remote `heavy`, here `light` |

Without arguments the script reads `.env`: `light` when `REMOTE_PROFILE` is set, else `single`. If
Ollama already runs as a service (systemd, the macOS app), the script cannot change its environment:
it pulls through it and prints the settings to apply.[^setup] See what a profile resolves to with
`python3 src/nexo/local_profiles.py plan m1-max-64 heavy`.

# How each setting is resolved

For every role setting (`AGENT_MODEL`, `ROUTER_BASE_URL`, ...), the first match wins:[^config]

1. Set explicitly in the environment or `.env`.
2. The profile, when `LOCAL_PROFILE` is set.
3. The `VLLM_*` default (the GPU server setup).

This is per field: `AGENT_MODEL=my-model` keeps the profile's agent URL. Profiles also default
`LLM_TIMEOUT_S` (300), `AGENT_TIMEOUT_S` (900) and `EXTRACTION_CONCURRENCY`, unless set.

# Who listens where

Everything binds `127.0.0.1`; nothing is exposed on the network.

| Service | Listens on | Nexo native | Nexo container, macOS | Nexo container, Linux |
|---|---|---|---|---|
| This machine's Ollama | `127.0.0.1:11434` | `localhost:11434` | `host.containers.internal:11434` | `127.0.0.1:11434` |
| Remote Ollama | remote `127.0.0.1:11434`, tunneled here to `127.0.0.1:11435` | `localhost:11435` | `host.containers.internal:11435` | `127.0.0.1:11435` |
| Neo4j | `127.0.0.1:7687` | `localhost:7687` | `neo4j:7687` | `127.0.0.1:7687` |
| OTLP (the collector) | `127.0.0.1:4318` | `localhost:4318` | `otel-collector:4318` | `127.0.0.1:4318` |
| SigNoz UI (browser; Nexo does not call it) | `127.0.0.1:8080` | http://localhost:8080 | - | - |
| Langfuse UI, add-on (browser) | `127.0.0.1:3000` | http://localhost:3000 | - | - |

`.env` holds the native values; the compose files set the container values.[^compose] On Linux,
rootless Podman cannot reach host loopback through `host.containers.internal`, so
`docker-compose.linux.yml` puts the Nexo container on the host network.[^override] The remote machine
is reached only through `ssh -N -L 11435:127.0.0.1:11434 you@remote`: Ollama has no authentication,
SSH provides it.

# Check

1. `nexo models` prints the model and server each role resolved to (no secrets).
2. `nexo models --check` (or `make check`) asks each server whether it serves that model, and sends the JSON roles
   (extraction, router, judge) one tiny schema-constrained request; it exits 1 if either fails.[^models]
3. `ollama ps` on each model machine lists the resident models (100% GPU on a Mac).
4. `NEXO_LIVE=1 pytest -m live -v` exercises the real models.

# When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `unreachable ... Connection refused` on 11434 | Ollama is not running | Run `ollama-setup.sh`, or start the service. |
| The same on 11435 | The SSH tunnel is down | Re-run the `ssh -N -L ...` command. |
| Works natively, unreachable from the Nexo container on Linux | The macOS URLs are in use | Add `-f docker-compose.linux.yml`. |
| `model not served here` | Role sets do not match, the model was not pulled, or an explicit `*_MODEL` has a typo | Re-run the setup with the role set from the table; the output lists what the server has. |
| `structured output failed` (e.g. `HTTP 501`) | The server cannot constrain output to a JSON schema; Ollama MLX builds (`-mlx`, `-nvfp4` and some default tags on Apple Silicon) cannot | Use a GGUF tag such as `qwen3.5:9b-q4_K_M`; the profiles pin these. |
| Checks pass, but long chunks or later sections are ignored | Ollama's 4096-token default context | Apply the printed `OLLAMA_CONTEXT_LENGTH` to the service and restart it. |
| Changed the embedding model; search results are poor | Old vectors from another model | Re-ingest every document. |

[^profiles]: Hardware profile catalog
[^config]: Settings and profile resolution
[^setup]: Ollama setup script
[^compose]: Docker Compose stack
[^override]: Linux compose override
[^models]: Model resolution and server check (nexo models)
