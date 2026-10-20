---
type: Playbook
title: Local on Apple Silicon
description: Run Nexo on one or two Apple Silicon Macs, with services in Podman or fully native, choosing models with a hardware profile such as m1-max-64.
resource: ../../src/nexo/local_profiles.py
tags: [operations, local, apple-silicon, ollama, podman]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profile catalog
  - id: setup
    resource: ../../deploy/local/ollama-setup.sh
    title: Ollama setup script
  - id: env
    resource: ../../deploy/local/local.env.example
    title: Local Nexo environment
  - id: compose
    resource: ../../docker-compose.yml
    title: Docker Compose stack
  - id: ollama
    resource: ../references/ollama.md
    title: Ollama
  - id: live
    resource: ../../tests/test_live_models.py
    title: Live model checks
---

# Choose

How the pieces fit together (roles, the profile catalog, ports): [How local setup works](local-setup.md).
Two independent choices:

| | **Podman path** | **Native path** |
|---|---|---|
| Models | Ollama, native (Metal GPU) | Ollama, native (Metal GPU) |
| Neo4j | container (`neo4j:5.26-community`) | Homebrew (`neo4j` 2026.09) |
| Telemetry | collector (strips content) + Jaeger containers | optional Jaeger binary, or none |
| Nexo | container (`--profile app`) or `pip install` | `pip install` |
| Good for | matching the server setup; clean teardown | fewest moving parts; no VM |

| | **One Mac** | **Two Macs** |
|---|---|---|
| `.env` | `LOCAL_PROFILE=m1-max-64` | `LOCAL_PROFILE=m5-pro-24` + `REMOTE_PROFILE=m1-max-64` |
| Serves | every role on this Mac | remote Mac: agent + extraction; this Mac: router, embeddings, judge, Nexo |

Containers on macOS cannot use the GPU, so the models are native in both paths; containers reach the
host's Ollama and SSH tunnel through `host.containers.internal`, which reaches services bound to the
host's `127.0.0.1` (verified with Podman 6.1 on macOS).[^compose]

# Hardware profiles

A profile is `<chip>-<memory GB>`: `m1-pro-16`, `m5-pro-24`, `m1-max-32`, `m1-max-64`, `m2-ultra-192`.
Memory picks the largest tier that fits; the chip is informational. The setup script and Nexo read
the same catalog, and explicit settings win over it: see
[one catalog, two readers](local-setup.md#one-catalog-two-readers).[^profiles]

| Tier | One Mac (`single`) | Agent Mac (`heavy`) | Nexo Mac (`light`) | Context |
|---|---|---|---|---|
| 16 GB | `qwen3.5:9b` for all chat roles, `bge-m3`, no judge (≈7.8 GB) | `qwen3.5:9b` | `qwen3:4b-instruct-2507`, `bge-m3`, no judge | 8K |
| 24 GB | `qwen3.5:9b` for all chat roles, `bge-m3`, no judge (≈7.8 GB) | `qwen3.5:9b` | `qwen3:4b-instruct-2507`, `bge-m3`, `llama3.1:8b` judge (≈8.6 GB) | 16K |
| 32 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M` for all chat roles, `bge-m3`, no judge (≈20 GB) | `qwen3:30b-a3b-instruct-2507-q4_K_M` | as 24 GB | 16K |
| 48 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M`, router `qwen3:4b-instruct-2507`, `llama3.1:8b`, `bge-m3` (≈28 GB) | `qwen3:30b-a3b-instruct-2507-q4_K_M` | as 24 GB | 32K |
| 64 GB | `qwen3.6:35b-a3b-q4_K_M`, router `qwen3:4b-instruct-2507`, `llama3.1:8b`, `bge-m3` (≈33 GB) | `qwen3.6:35b-a3b-q4_K_M` | as 24 GB | 32K |

Sizes are Ollama library download sizes; KV cache and the OS need the rest. `qwen3.5:9b` and
`qwen3.6:35b-a3b-q4_K_M` are thinking models: the profile sets reasoning off for their JSON roles
(`EXTRACTION_REASONING_EFFORT=none`, ...) and leaves the agent's reasoning on. Inspect any profile with
`python3 src/nexo/local_profiles.py plan m1-max-64 heavy`.

# Podman path

1. `brew install ollama podman` and `podman machine init` (first time only), `podman machine start`.
2. Models, per Mac (see "Two Macs" below for the second Mac):
   `deploy/local/ollama-setup.sh m1-max-64` - pulls and preloads the profile's models and starts
   Ollama with the right context length, keep-alive and KV-cache settings, bound to `127.0.0.1`.[^setup]
3. `cp deploy/local/local.env.example .env`, set the profile(s) and `NEO4J_PASSWORD`.[^env]
4. `podman compose up -d neo4j otel-collector jaeger` - Neo4j on `127.0.0.1:7687`, traces at
   http://localhost:16686, metrics at http://localhost:8889/metrics.
5. Run Nexo either natively (`pip install -e .`, then `nexo ...`) or in a container:
   `podman compose --profile app run --rm nexo ingest /corpus/memo.md` with documents in `./corpus`.
   The container's `LOCAL_BASE_URL`/`REMOTE_BASE_URL` already point at `host.containers.internal`.

# Native path

1. `brew install ollama neo4j` (Neo4j 2026.09 Community, Java 21; Nexo's Neo4j tests pass on it).
2. `neo4j-admin dbms set-initial-password <password>` then `brew services start neo4j`.
3. Models: `deploy/local/ollama-setup.sh m1-max-64`.
4. `cp deploy/local/local.env.example .env`, set the profile(s) and `NEO4J_PASSWORD`.
5. Telemetry (optional): download the Jaeger v2 `darwin-arm64` release binary and run `./jaeger`; it
   accepts OTLP on `localhost:4318` and serves the UI on http://localhost:16686. Without the collector
   there is no second content filter, so keep
   `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`. Or unset
   `OTEL_EXPORTER_OTLP_ENDPOINT` to rely on logs only.
6. `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`, then `nexo ...`.

# Two Macs

Applies to either path. Example: M1 Max 64 GB as the agent Mac, M5 Pro 24 GB running Nexo.

```
 M1 Max (REMOTE_PROFILE=m1-max-64)         M5 Pro (LOCAL_PROFILE=m5-pro-24)
 ┌───────────────────────────┐   SSH       ┌──────────────────────────────────────────┐
 │ Ollama 127.0.0.1:11434    │◄─tunnel─────┤ localhost:11435 ─► agent, extraction    │
 │  agent + extraction model │             │ Ollama 127.0.0.1:11434                   │
 └───────────────────────────┘             │  router, embeddings, judge               │
                                           │ Neo4j, telemetry, Nexo (Podman or native)│
                                           └──────────────────────────────────────────┘
```

1. M1 Max: `deploy/local/ollama-setup.sh m1-max-64 heavy`; enable Remote Login.
2. M5 Pro: `deploy/local/ollama-setup.sh` (reads `LOCAL_PROFILE` and `REMOTE_PROFILE` from `.env` and
   serves the `light` set).
3. M5 Pro: `ssh -N -L 11435:127.0.0.1:11434 you@m1-max.local` and keep it running.

Never bind Ollama to `0.0.0.0` on a shared network: it has no authentication.

# Verify

`nexo models --check` confirms each role's server answers and serves its model
([what failures mean](local-setup.md#when-it-does-not-work)). `NEXO_LIVE=1 pytest -m live -v` checks the real models: schema-valid JSON from the extraction and router
models, embeddings that rank related text (English and Spanish) above unrelated text, and an agent model
that calls its tools over the OpenAI protocol.[^live] `ollama ps` on each Mac should list the profile's
models at 100% GPU.

# Gotchas

- **Context length.** Below about 23 GiB of GPU memory Ollama defaults to a 4096-token context and
  truncates longer prompts silently; the setup script sets `OLLAMA_CONTEXT_LENGTH` from the profile.
  If Ollama already runs (e.g. the menu-bar app), the script prints the settings instead: apply them
  with `launchctl setenv KEY VALUE` and reopen the app.
- **GPU memory limit.** If a model does not fit, raise macOS's GPU wired-memory limit until reboot with
  `sudo sysctl iogpu.wired_limit_mb=<MB>`, or pick a smaller profile.
- **Speed.** Expect tens of tokens per second; profiles raise `LLM_TIMEOUT_S` (300) and
  `AGENT_TIMEOUT_S` (900) and lower `EXTRACTION_CONCURRENCY` to the tier's parallelism.
- **Changing embedding models** requires re-ingesting everything.[^ollama]

On Linux, or to use a Linux GPU machine as the remote, see [Local on Linux](local-linux.md).
See [ADR-0012](../decisions/adr-0012-local-apple-silicon.md).

[^profiles]: Hardware profile catalog
[^setup]: Ollama setup script
[^env]: Local Nexo environment
[^compose]: Docker Compose stack
[^ollama]: Ollama
[^live]: Live model checks
