---
type: Deployment Model
title: How local setup works
description: The mechanism behind local runs - one setup file (nexo.yaml) rendered by nexo setup, five model roles, one profile catalog read by both the setup script and Nexo, who listens where, and how to check it.
resource: ../../src/nexo/setup/commands.py
tags: [operations, local, ollama, profiles, podman, setup]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T16:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: setup-cli
    resource: ../../src/nexo/setup/commands.py
    title: nexo setup
  - id: schema
    resource: ../../src/nexo/setup/schema.py
    title: The setup file schema
  - id: presets
    resource: ../../examples/setups/mac-single/nexo.yaml
    title: Preset for one Mac
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
    title: Generated compose file (mac-single preset)
  - id: models
    resource: ../../src/nexo/llm.py
    title: Model resolution and server check (nexo models)
  - id: adr-0016
    resource: ../decisions/adr-0016-declarative-setup-file.md
    title: "ADR-0016: A declarative setup file"
---

# One setup file

`nexo.yaml` describes a deployment: where it runs, how models are served and which add-ons are on.
`make env` writes it from a preset; `make up` runs it.[^setup-cli] The idea follows SigNoz's foundry:
one declarative file, a lock and rendered plain files.[^adr-0016]

```yaml
apiVersion: nexo/v1alpha1
kind: Setup
spec:
  target: { mode: compose, engine: auto, host: auto }   # podman or docker; macos or linux
  models:
    provider: ollama                                    # ollama | vllm | openai-compatible
    local:  { profile: m5-pro-24 }                      # this machine's roles
    remote: { profile: m1-max-64, port: 11435 }         # optional second machine (SSH tunnel)
    roles:  { agent: { model: qwen3:30b-a3b-instruct-2507-q4_K_M } }   # explicit roles win
  components:                                           # core: neo4j, collector, signoz
    neo4j: { env: { NEO4J_server_memory_heap_max__size: 1G } }
  addons:                                               # off unless enabled (hostMetrics: on)
    langfuse: { enabled: true }
    jaeger: { enabled: false }
    langsmith: { enabled: false, project: nexo }
    hostMetrics: { enabled: true }
  telemetry: { logs: otlp }
  patches:                                              # RFC 6902, applied last
    - target: compose.yaml
      operations: [{ op: add, path: /services/neo4j/mem_limit, value: 3g }]
```

Each component also takes `image`, `images` (per service), `env` and, for the collector, `config`
(merged into its config file). Unknown keys, impossible combinations and invalid profiles are
rejected with the field named.[^schema]

| Setup | Preset (`make env PRESET=...`) |
|---|---|
| One Apple Silicon Mac | `mac-single` (the default on macOS) |
| Two machines over an SSH tunnel | `mac-two-machines` |
| Linux, CPU only | `linux-cpu` (the default on Linux) |
| Linux, one GPU, Ollama | `linux-gpu` |
| Linux, NVIDIA GPUs, vLLM | `linux-vllm` |

Kubernetes comes after the compose target. Presets live in `examples/setups/`.[^presets]

# What the targets do

| Target | Runs | Effect |
|---|---|---|
| `make env` | `nexo setup init`, `nexo setup secrets` | `nexo.yaml` from a preset (kept if it exists); `.env` from the template with random secrets for the enabled components (existing values kept) |
| `make lock` | `nexo setup lock` | `nexo.lock`: the components on, every service's image and each role's model. Pins stay when `nexo.yaml` changes; `UPGRADE=1` moves them to the current defaults |
| `make render` | `nexo setup render` | `.nexo/render/`: `compose.yaml` (only the enabled services), `otel-collector.yaml` (the base plus each add-on's pipeline), `nexo.env` |
| `make check` | `nexo setup check` | Preflight (engine, secrets, Ollama, memory budget, Podman VM limit), then each role's server and model |
| `make up` | `nexo setup up` | Secrets, render, preflight, `ollama-setup.sh` for this machine's roles, `compose up -d` |
| `make down` / `make app` | `nexo setup down` / `compose run nexo` | Stop (volumes kept) / the CLI in a container |

`LANGFUSE=1`, `JAEGER=1` and `LANGSMITH=1` turn an add-on on for one run without editing
`nexo.yaml`. `nexo.yaml`, `nexo.lock` and `.nexo/` are git-ignored. The committed
`docker-compose.yml` is the `mac-single` preset rendered with repo-relative paths (`make
compose-file`), for plain `podman compose up -d`; a test fails when it is stale.[^compose]

# Five model roles

| Role | Used for | One machine | Two machines |
|---|---|---|---|
| agent | The investigator's multi-turn tool calls | this machine | remote |
| extraction | Triples and entity resolution during ingest | this machine | remote |
| router | Intake validation | this machine | this machine |
| judge | Generation scores in `nexo eval`; off where the tier has no room | this machine | this machine |
| embedding | Chunk and query vectors | this machine | this machine |

Models always run natively in Ollama: containers on macOS cannot use the GPU. Step-by-step:
[Local on Apple Silicon](local-apple-silicon.md), [Local on Linux](local-linux.md).

# One catalog, two readers

```
                    src/nexo/local_profiles.py
              profile -> memory tier -> models per role set
                 │                                   │
   ollama-setup.sh PROFILE [ROLE_SET]          Nexo settings
   on each model machine:                      on the machine running Nexo:
   - pulls and preloads the models             - model and server URL per role
   - sizes Ollama: context length,             - reasoning off for JSON roles on thinking models
     parallelism, keep-alive, KV cache         - longer timeouts, extraction concurrency
```

Both read the same catalog,[^profiles] so they agree as long as the role sets match. `make up` runs
the script with `single` (one machine) or `light` (two machines); on the remote machine run
`ollama-setup.sh m1-max-64 heavy` yourself (`make check` prints it). If Ollama already runs as a
service (systemd, the macOS app), the script pulls through it and prints the settings to
apply.[^setup] See what a profile resolves to with `python3 src/nexo/local_profiles.py plan m1-max-64 heavy`.

# How each setting is resolved

For every setting, the first match wins:[^config]

1. A real environment variable.
2. `.nexo/render/nexo.env`, rendered from `nexo.yaml` (profiles, endpoints, log export, explicit roles).
3. `.env` (secrets, personal overrides such as `AGENT_MODEL`).
4. The profile, for role settings not set above.
5. The `VLLM_*` default (the GPU server setup).

This is per field: `AGENT_MODEL=my-model` keeps the profile's agent URL. Profiles also default
`LLM_TIMEOUT_S` (300), `AGENT_TIMEOUT_S` (900) and `EXTRACTION_CONCURRENCY`, unless set. Without
`nexo setup` (native Neo4j, no containers), set `LOCAL_PROFILE` and the endpoints in `.env`.

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

`nexo.env` holds the native values; the render gives the Nexo container its own. On Linux, rootless
Podman cannot reach host loopback through `host.containers.internal`, so the render puts the Nexo
container on the host network (with Docker on macOS it uses `host.docker.internal`). The remote
machine is reached only through `ssh -N -L 11435:127.0.0.1:11434 you@remote`: Ollama has no
authentication, SSH provides it.

# Check

1. `make check` runs the preflight, then `nexo models --check`: each server is asked whether it
   serves its model, and the JSON roles (extraction, router, judge) get one tiny schema-constrained
   request; it exits 1 if either fails.[^models]
2. `nexo models` prints the model and server each role resolved to (no secrets).
3. `ollama ps` on each model machine lists the resident models (100% GPU on a Mac).
4. `NEXO_LIVE=1 pytest -m live -v` exercises the real models.

# When it does not work

| Symptom | Cause | Fix |
|---|---|---|
| `nexo.yaml: ...` error naming a field | A typo or an impossible combination | Fix that field; the presets show valid files. |
| `FAIL secrets: set in .env: HF_TOKEN` | A key only you can provide | Add it to `.env`. |
| `FAIL podman vm` | The enabled containers need more than the Podman VM has | `podman machine set --memory 10240`, or turn off an add-on. |
| `unreachable ... Connection refused` on 11434 | Ollama is not running | `make up` (or `make models`), or start the service. |
| The same on 11435 | The SSH tunnel is down | Re-run the `ssh -N -L ...` command. |
| `model not served here` | Role sets do not match, the model was not pulled, or an explicit `*_MODEL` has a typo | Re-run the setup with the role set from the table; the output lists what the server has. |
| `structured output failed` (e.g. `HTTP 501`) | The server cannot constrain output to a JSON schema; Ollama MLX builds (`-mlx`, `-nvfp4` and some default tags on Apple Silicon) cannot | Use a GGUF tag such as `qwen3.5:9b-q4_K_M`; the profiles pin these. |
| Checks pass, but long chunks or later sections are ignored | Ollama's 4096-token default context | Apply the printed `OLLAMA_CONTEXT_LENGTH` to the service and restart it. |
| Changed the embedding model; search results are poor | Old vectors from another model | Re-ingest every document. |

[^setup-cli]: nexo setup
[^schema]: The setup file schema
[^presets]: Preset for one Mac
[^profiles]: Hardware profile catalog
[^config]: Settings and profile resolution
[^setup]: Ollama setup script
[^compose]: Generated compose file (mac-single preset)
[^models]: Model resolution and server check (nexo models)
[^adr-0016]: "ADR-0016: A declarative setup file"
