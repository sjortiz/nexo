---
type: Deployment Model
title: How local setup works
description: The mechanism behind local runs - one setup file (nexo.yaml) rendered by nexo setup, five model roles, one profile catalog read by both the setup script and Nexo, who listens where, and how to check it.
resource: ../../src/nexo/setup/commands.py
tags: [operations, local, ollama, profiles, podman, setup]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T21:00:00Z }
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
  - id: adr-0017
    resource: ../decisions/adr-0017-secrets-from-a-provider.md
    title: "ADR-0017: Secrets from a provider, not a file"
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
  target: { mode: compose, engine: auto, host: auto }   # compose | native; podman or docker; macos or linux
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
  telemetry: { logs: otlp, queryText: false, content: internal }  # content: none|public|internal|pii|phi
  ports: { neo4jBolt: 7687, signoz: 8080 }              # host ports; move them for a second stack
  review: on-findings                                   # on-findings (default) | always | never
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
| One Apple Silicon Mac, no containers | `mac-native` (`target.mode: native`) |
| Two machines over an SSH tunnel | `mac-two-machines` |
| Linux, CPU only | `linux-cpu` (the default on Linux without an NVIDIA GPU) |
| Linux, one GPU, Ollama | `linux-gpu` (the default with one) |
| Linux, no containers | `linux-native` (`target.mode: native`) |
| Linux, NVIDIA GPUs, vLLM | `linux-vllm` |

`make env` (`nexo setup init`) fills in `models.local.profile` from this machine's chip and memory
(Apple Silicon), GPU VRAM or RAM (Linux); `PROFILE=` (`--profile`) chooses another. `target.mode:
native` runs no containers: Neo4j is this machine's own service, telemetry export is off and
`.nexo/render/` holds only `nexo.env`; add-ons and patches need `compose`
([ADR-0020](../decisions/adr-0020-native-target-mode.md)). Kubernetes comes after the compose
target. Presets live in `examples/setups/`.[^presets]

# What the targets do

| Target | Runs | Effect |
|---|---|---|
| `make env` | `nexo setup init`, `nexo setup secrets` | `nexo.yaml` from a preset (kept if it exists); `.env` from the template for personal settings; random secrets for the enabled components (existing values kept) |
| `make lock` | `nexo setup lock` | `nexo.lock`: the components on, every service's image and each role's model. Pins stay when `nexo.yaml` changes; `UPGRADE=1` moves them to the current defaults |
| `make render` | `nexo setup render` | `.nexo/render/`: `compose.yaml` (only the enabled services), `otel-collector.yaml` (the base plus each add-on's pipeline), `nexo.env` |
| `make check` | `nexo setup check` | Preflight (engine, secrets, the [review policy](human-review.md#review-policy), Ollama, the profile against this machine's memory, memory budget, Podman VM limit), then each role's server and model; a failed memory check stops `check` and `up` unless `FORCE=1` |
| `make up` | `nexo setup up` | Secrets, render, preflight, `ollama-setup.sh` for this machine's roles, `compose up -d` (removing add-ons a previous run enabled), then waits until every service runs; fails with the service's last log lines when one exits |
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

The `light` set is not the `single` set without the heavy models: on a 24 GB Mac it adds a
separate router (qwen3 4B, 2.5 GB) and the judge (Llama 3.1 8B, 4.9 GB), so moving from one
machine to two pulls about 7.4 GB more. `make check` lists the models this machine serves and
the ones `make up` will pull.

# How each setting is resolved

For every setting, the first match wins:[^config]

1. A real environment variable.
2. The secrets provider, for the generated secrets Nexo uses (`NEO4J_PASSWORD`, the vLLM keys).
3. `.nexo/render/nexo.env`, rendered from `nexo.yaml` (profiles, endpoints, log export, explicit roles).
4. `.env` (personal overrides such as `AGENT_MODEL`, keys only you have such as `HF_TOKEN`).
5. The profile, for role settings not set above.
6. The `VLLM_*` default (the GPU server setup).

# Where secrets live

Generated secrets (Neo4j, SigNoz and Langfuse passwords and keys) never sit in a file you copy
between machines.[^adr-0017] By default they live in `~/.config/nexo/config` (a `[stack]`
section, owner-only), which every checkout of the stack on this machine shares, so one stack per
machine works from any checkout. `spec.secrets.provider` opts in to the OS keychain (`make install-keychain` first), SOPS or Vault; on
a cluster, `file` with a `path` reads what the platform mounts, read-only and never as environment
variables. Locally, `nexo setup` hands the values to compose through the environment. The SigNoz and
Langfuse logins are in `~/.config/nexo/config` under `[nexo]`. Secrets found in an older `.env` move into the store on the next
`make env`.

This is per field: `AGENT_MODEL=my-model` keeps the profile's agent URL. Profiles also default
`LLM_TIMEOUT_S` (300), `AGENT_TIMEOUT_S` (900) and `EXTRACTION_CONCURRENCY`, unless set. Native
Neo4j and no containers is `target.mode: native`, not hand-edited `.env` settings.
`nexo config --explain` prints each effective setting and where it came from (environment, secrets
store, `.nexo/render/nexo.env`, `.env`, derived from a profile, or default), secret values redacted.
`nexo.env` records the `nexo.yaml` it was rendered from; when that file has changed since, every
`nexo` command except `nexo setup` warns until `make render` (or `make up`) runs.

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

These are the defaults. `spec.ports` moves each published port (`neo4jBolt`, `neo4jHttp`, `otlp`,
`metrics`, `signoz`, `langfuse`, `langfuseStorage`, `jaeger`, `agentInbox`, `reviewInbox`, `vllm`,
`vllmEmbedding`); `nexo.env`,
the lock and the printed links follow, and ports inside the stack stay. A second stack on one
machine needs its own ports and `metadata.name`.

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
| `FAIL secrets: ...; set: HF_TOKEN` | A key only you can provide, or a read-only mount that lacks a secret | Add it to `.env`, or to the mounted secret. |
| `FAIL secrets: secrets file ... not found`, `keychain ...` or `vault ...: HTTP 403` | The provider cannot be read (no mount, a locked keychain, a wrong token, no `sops` key) | Mount it, unlock it or fix its credentials; the message names the provider. |
| `FAIL neo4j: Neo4j refused the password` | The `neo4j-data` volume was made with other secrets | Put the old password back in the store, or delete the named volume (its data goes). |
| `FAIL neo4j: no Neo4j at bolt://localhost:7687` (native) | This machine's Neo4j is not running | `brew services start neo4j` (Linux: `sudo systemctl start neo4j`). |
| `FAIL neo4j: Neo4j at ... refused the password in ...` (native) | Neo4j was given another password | A Neo4j that never started: `make neo4j-init`; one in use: set its password to the stored one. |
| `todo model agent: ... not pulled yet` | Before the first `make up` | Nothing: `up` pulls it. |
| `WARN ollama context: ... runs ... with 4096 tokens of context` | Ollama started outside the setup script (the macOS app, a systemd unit) with its defaults | `make models APPLY=1` on macOS, the printed systemd drop-in on Linux; restart Ollama. |
| `... changed since .nexo/render/nexo.env was rendered` | `nexo.yaml` was edited after the last render | `make render` (or `make up`). |
| `FAIL ports: in use: 7687 (nexo-neo4j-1, stack nexo)` | Another stack or process holds a port this setup publishes | Set `spec.ports` (and another `metadata.name` for a second stack); `FORCE=1` to go ahead anyway. |
| `FAIL profile: ... is for 64 GB; this machine has 24 GB` | The profile names more memory than the machine has | Set the profile for this machine; `FORCE=1` to go ahead anyway. |
| `FAIL host: rendered for linux; this machine runs macos` | A Linux preset on a Mac (or the reverse) | Use a preset for this OS, or set `target.host`; `FORCE=1` to render anyway. |
| `FAIL gpu` | A `gpu-*` profile without `nvidia-smi`/`rocm-smi`, or vLLM without NVIDIA GPUs usable by containers | Install the driver tools; for vLLM the NVIDIA Container Toolkit (Docker) or a CDI spec (Podman). |
| `signoz did not start` ... `refused the password from your secrets` | A data volume made with other secrets (a regenerated password) | Put the old value back in the store, or delete the named volume (its data goes) and `make up` again. |
| `FAIL memory` | Models plus containers need more than 85% of this machine's memory | A smaller profile, fewer add-ons, or `FORCE=1`. |
| `FAIL podman vm` | The enabled containers need more than 90% of the Podman VM | The printed `podman machine stop && podman machine set --memory N && podman machine start`, turn off an add-on, or `FORCE=1`. |
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
[^adr-0017]: "ADR-0017: Secrets from a provider, not a file"
