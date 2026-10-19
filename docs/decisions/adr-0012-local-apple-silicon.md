---
type: Architecture Decision
title: "ADR-0012: Ollama for local runs, with model roles split across Macs"
description: Run every model natively through Ollama on Apple Silicon and let each Nexo role (agent, extraction, router, judge, embeddings) point at its own host and model.
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
  falling back to the default `VLLM_*` endpoint. The large agent/extraction model runs on the M1 Max;
  router, embeddings, judge, Neo4j and Nexo run on the M5 Pro.
- Connect the Macs with an SSH tunnel; Ollama stays bound to localhost.
- `NEXO_LIVE=1 pytest -m live` verifies a model set before use.[^playbook]

# Options considered

| Option | For | Against |
|---|---|---|
| **Ollama** (chosen) | One daemon for chat, JSON schema, tools and embeddings; simple model management. | No auth; context length set only server-side. |
| LM Studio | Strong MLX engine. | Closed source; auto-evicts JIT-loaded models. |
| vllm-metal | Closest to production vLLM. | One server per model; embeddings experimental. |
| llama.cpp / mlx_lm servers | Lightweight. | Several processes, or missing structured output/embeddings (mlx_lm). |
| Everything on one Mac | No network. | The 24 GB Mac cannot hold a 19-24 GB agent model plus the rest. |

# Consequences

- Local model quality differs from Llama 3.3 70B on vLLM: evaluation results are per model set.
- Role overrides also serve production (e.g. a smaller extraction model, a judge on another server).

[^ollama]: Ollama
[^playbook]: Local on Apple Silicon
