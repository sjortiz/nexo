---
type: Architecture Decision
title: "ADR-0012: Ollama for local runs, hardware profiles, roles split across Macs"
description: Run every model natively through Ollama on Apple Silicon, choose models with a hardware profile (e.g. m1-max-64), and let roles run on one Mac or two, with services in Podman or native.
tags: [adr, local, apple-silicon, ollama, inference]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: ollama
    resource: ../references/ollama.md
    title: Ollama
  - id: playbook
    resource: ../operations/local-apple-silicon.md
    title: Local on Apple Silicon
---

# Context

Development and evaluation should not need a GPU server. The team has two Apple Silicon Macs (an M1 Max
and a 24 GB M5 Pro) with unified memory; containers on macOS cannot use the GPU. Nexo needs three
capabilities from its model server: JSON-schema structured output, streamed tool calls, and embeddings.

# Decision

- Use Ollama natively on each Mac: one daemon provides all three capabilities and keeps several models
  resident in unified memory.[^ollama]
- Add per-role endpoints (`AGENT_*`, `EXTRACTION_*`, `ROUTER_*`, `JUDGE_*`, plus `EMBEDDING_*`), each
  falling back to the default `VLLM_*` endpoint.
- Hardware profiles (`LOCAL_PROFILE`, `REMOTE_PROFILE` = `<chip>-<memory GB>`) fill those settings from one
  catalog shared with the setup script; explicit settings win. One Mac serves every role, or a second Mac
  serves the agent and extraction models.
- Two ways to run the services: Podman (Neo4j, collector, Jaeger, optionally Nexo in containers that reach
  host Ollama via `host.containers.internal`) or native (Homebrew Neo4j, optional Jaeger binary).
- Connect the Macs with an SSH tunnel; Ollama stays bound to localhost.
- `NEXO_LIVE=1 pytest -m live` verifies a model set before use.[^playbook]

# Options considered

| Option | For | Against |
|---|---|---|
| **Ollama** (chosen) | One daemon for chat, JSON schema, tools and embeddings; simple model management. | No auth; context length set only server-side. |
| LM Studio | Strong MLX engine. | Closed source; auto-evicts JIT-loaded models. |
| vllm-metal | Closest to production vLLM. | One server per model; embeddings experimental. |
| llama.cpp / mlx_lm servers | Lightweight. | Several processes, or missing structured output/embeddings (mlx_lm). |
| One Mac only | No network, simplest. | Kept as an option (`LOCAL_PROFILE` alone); a 24 GB Mac then runs a smaller agent model (9B) than a 32-64 GB Mac. |

# Consequences

- Local model quality differs from Llama 3.3 70B on vLLM: evaluation results are per model set.
- Role overrides also serve production (e.g. a smaller extraction model, a judge on another server).
- The catalog pins model tags and sizes; when Ollama's library changes, update `local_profiles.py`
  (tests check every tier fits its memory and that the playbook lists every model).

[^ollama]: Ollama
[^playbook]: Local on Apple Silicon
