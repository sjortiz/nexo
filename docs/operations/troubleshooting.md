---
type: Playbook
title: Troubleshooting
description: Symptoms, causes and fixes for common failures.
tags: [operations, troubleshooting, oncall]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: compose
    resource: ../../docker-compose.yml
    title: Docker Compose stack
  - id: llm
    resource: ../../src/nexo/llm.py
    title: Model clients
---

Local models (Ollama, profiles, SSH tunnel): run `nexo models --check` and see
[How local setup works](local-setup.md#when-it-does-not-work).

# Agent

| Symptom | Cause | Fix |
|---|---|---|
| Answer arrives with no tool calls and `no_citations` | vLLM started without tool calling | Add `--enable-auto-tool-choice --tool-call-parser llama3_json`.[^compose] |
| `stop_reason: limit_turns` often | Budget too tight or queries too broad | Raise `AGENT_MAX_TURNS`; check traces for repeated failing queries. |
| `ungrounded_citations` | The model cited ids it saw in graph rows but never fetched | Reject; if frequent, the model is skipping `fetch_vector_chunks` and the prompt needs tuning. |
| Tool returns `read-only access: SET is not allowed` for a read query | A property or label named like a keyword (`n.set`) | Rename the property, or alias it in the query. |
| `investigation exceeded 300s` | Slow vLLM or long loops | Check vLLM queue depth; lower `AGENT_MAX_TURNS` or raise `AGENT_TIMEOUT_S`. |

# Router

| Symptom | Cause | Fix |
|---|---|---|
| Every request `rejected: router unavailable (...)` | vLLM down, or guided decoding rejecting the schema | Check `VLLM_BASE_URL`; test a `response_format` json_schema request directly. |

# Ingestion

| Symptom | Cause | Fix |
|---|---|---|
| Many `evidence_not_in_chunk` drops | The model paraphrases quotes | Expected at low rates. If high, review the extraction prompt; disabling `REQUIRE_GROUNDED_EVIDENCE` trades accuracy for recall. |
| Embedding calls rejected with token-id errors | Client pre-tokenizes for OpenAI | `check_embedding_ctx_length=False` is set in `build_embeddings`; check you are not overriding it.[^llm] |
| `failed_chunk_ids` non-empty | Invalid JSON after retries, or vLLM unreachable | Re-run the same ingest; chunk ids are stable and the store keeps the text. |

# Graph quality

| Symptom | Cause | Fix |
|---|---|---|
| The same company appears as two nodes | Names share no token and no alias was stated | Add the alias to the source text or glossary; check `MENTIONED_IN.resolution` counts in the ingestion report; see [ADR-0010](../decisions/adr-0010-deferred-graph-enrichment.md) for embedding blocking. |
| Two different entities merged | Model arbiter over-merged | Find the edge with `MENTIONED_IN {resolution: 'llm'}` and re-ingest that document with `ENTITY_RESOLUTION=deterministic`; tighten descriptions. |
| Many `RELATED_TO` edges | Ontology missing synonyms | Add the frequent `predicate` values as synonyms or inverses in `ONTOLOGY_PATH`, then re-ingest. |

# Review

| Symptom | Cause | Fix |
|---|---|---|
| `thread ... is not awaiting review` | Wrong thread id, already decided, or in-memory checkpointer restarted | Use `nexo status <thread_id>`; use `sqlite` or `postgres` checkpoints. |
| Approval keeps returning `awaiting_review` | Blocking findings without acknowledgement | Add `--acknowledge-findings` after reading the findings. |

[^compose]: Docker Compose stack
[^llm]: Model clients
