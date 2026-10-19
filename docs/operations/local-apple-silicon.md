---
type: Playbook
title: Local on Apple Silicon
description: Run Nexo fully locally on one or two Apple Silicon Macs with Ollama, sharing unified memory and splitting model roles across machines.
resource: ../../deploy/local/ollama-setup.sh
tags: [operations, local, apple-silicon, ollama]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: setup
    resource: ../../deploy/local/ollama-setup.sh
    title: Ollama setup script
  - id: env
    resource: ../../deploy/local/nexo-two-macs.env.example
    title: Two-Mac Nexo environment
  - id: ollama
    resource: ../references/ollama.md
    title: Ollama
  - id: live
    resource: ../../tests/test_live_models.py
    title: Live model checks
---

# Topology

Models run natively on each Mac's GPU (Metal) through Ollama; containers on macOS cannot use the GPU, so
only Neo4j, the collector and Jaeger run in Podman. Each Nexo role points at its own endpoint
(see [configuration](configuration.md): `AGENT_*`, `EXTRACTION_*`, `ROUTER_*`, `JUDGE_*`,
`EMBEDDING_*`).[^env]

```
 M1 Max (32/64 GB)                         M5 Pro (24 GB)
 ┌───────────────────────────┐   SSH       ┌──────────────────────────────────────────┐
 │ Ollama 127.0.0.1:11434    │◄─tunnel─────┤ localhost:11435 ─► agent, extraction    │
 │  agent + extraction model │             │ Ollama 127.0.0.1:11434                   │
 └───────────────────────────┘             │  router, embeddings (bge-m3), judge      │
                                           │ Podman: Neo4j, OTEL collector, Jaeger    │
                                           │ Nexo (pip install, runs natively)        │
                                           └──────────────────────────────────────────┘
```

# Model sets

| Profile | Mac | Models (Ollama tags) | Approx. weights |
|---|---|---|---|
| `m1-max-32` | M1 Max 32 GB | `qwen3:30b-a3b-instruct-2507-q4_K_M` (agent + extraction) | 19 GB |
| `m1-max-64` | M1 Max 64 GB | `qwen3.6:35b-a3b-q4_K_M` (agent + extraction; set `STRUCTURED_REASONING_EFFORT=none`) | 24 GB |
| `m5-pro-24` | M5 Pro 24 GB | `qwen3:4b-instruct-2507` (router), `bge-m3` (embeddings), `llama3.1:8b` (judge) | 8.6 GB |
| `single-64` | one 64 GB Mac | `qwen3:30b-a3b-instruct-2507-q4_K_M`, `bge-m3`, `llama3.1:8b` | 25 GB |

The judge comes from a different model family than the generator to limit self-preference. Sizes are
download sizes from the Ollama library; the KV cache adds to them.[^ollama]

# Steps

1. **Both Macs:** `brew install ollama`.
2. **M1 Max:** `deploy/local/ollama-setup.sh m1-max-32` (or `m1-max-64`). It pulls and preloads the
   model, binds Ollama to `127.0.0.1` and sets context length, keep-alive and KV-cache quantization.[^setup]
3. **M5 Pro:** `deploy/local/ollama-setup.sh m5-pro-24`.
4. **M5 Pro, tunnel to the M1 Max:** `ssh -N -L 11435:127.0.0.1:11434 you@m1-max.local` (keep it
   running; enable Remote Login on the M1 Max).
5. **M5 Pro:** `cp deploy/local/nexo-two-macs.env.example .env`, set `NEO4J_PASSWORD`, then
   `podman machine start` and `podman compose up -d neo4j otel-collector jaeger`.
6. **Verify the models:** `NEXO_LIVE=1 pytest -m live -v`. It checks schema-valid JSON from the
   extraction and router models, embeddings that rank related text (English and Spanish) above
   unrelated text, and an agent model that calls its tools over the OpenAI protocol.[^live]
7. **Use it:** `nexo ingest ...`, `nexo ask ...`, `nexo review ...` as in the
   [request lifecycle](../architecture/request-lifecycle.md).

On a single Mac, skip the tunnel and point every role at `http://localhost:11434/v1`.

# Gotchas

- **Context length.** Below about 23 GiB of GPU memory, Ollama defaults to a 4096-token context and
  truncates longer prompts silently. The setup script sets `OLLAMA_CONTEXT_LENGTH`; the OpenAI API
  cannot change it per request.
- **Keep-alive.** The default unloads a model after 5 minutes; the script sets `OLLAMA_KEEP_ALIVE=-1`.
  `ollama ps` should show every model at 100% GPU.
- **Thinking models** (Qwen3.5/3.6) enforce the JSON schema only after their reasoning; set
  `STRUCTURED_REASONING_EFFORT=none` for the JSON roles. The agent keeps its reasoning.
- **No authentication.** Never bind Ollama to `0.0.0.0` on a shared network; use the SSH tunnel.
- **GPU memory limit.** If a model does not fit, macOS's GPU wired-memory limit can be raised until
  reboot with `sudo sysctl iogpu.wired_limit_mb=<MB>`.
- **Speed.** Expect tens of tokens per second, not a GPU server's hundreds: the env template raises
  `LLM_TIMEOUT_S` and `AGENT_TIMEOUT_S` and lowers `EXTRACTION_CONCURRENCY`.
- **Switching embedding models** (e.g. to `qwen3-embedding:0.6b`, also 1024 dimensions) requires
  re-ingesting everything.

See [ADR-0012](../decisions/adr-0012-local-apple-silicon.md) for the choice of Ollama.

[^setup]: Ollama setup script
[^env]: Two-Mac Nexo environment
[^ollama]: Ollama
[^live]: Live model checks
