---
type: Agent
title: Investigator agent
description: The Strands agent that traces relationships autonomously over the graph and chunk text, served by vLLM.
resource: ../../src/nexo/agents/investigator.py
tags: [module-b, strands, agent, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: investigator
    resource: ../../src/nexo/agents/investigator.py
    title: Investigator
  - id: llm
    resource: ../../src/nexo/llm.py
    title: build_strands_model
---

# Design

- **Goal, not workflow.** The system prompt states the goal (deep-dive into how the question's entities
  connect, directly and through multi-hop paths), the graph data model with the relation patterns
  present, which tool to use for paths and neighbours, example queries (alias lookup, timelines,
  neighbouring chunks), and how to judge evidence. It never lists steps.
- **Five tools.** [query_knowledge_graph](query-knowledge-graph.md),
  [find_paths and entity_neighbours](graph-lookup-tools.md),
  [fetch_vector_chunks](fetch-vector-chunks.md) and [search_chunks](search-chunks.md), built per
  investigation as closures over an `InvestigationContext` (budget, clearance, evidence ledger).
- **Fresh agent per request.** Strands agents hold conversation state; one is built per investigation,
  while the vLLM model client is shared.
- **Seeds for vector/hybrid.** The top `SEED_CHUNKS` (5) hits from the chunk store's search (hybrid BM25 +
  vector by default) under the request's filter are
  placed in the prompt inside `<passage>` tags. Seeds count as fetched evidence.
- **Untrusted content.** Tool output and seed passages are declared as data, never instructions.[^investigator]

# Model

`OpenAIModel` pointed at `VLLM_BASE_URL` with `AGENT_TEMPERATURE` 0.1 and `AGENT_MAX_TOKENS` 2048. vLLM
must run with `--enable-auto-tool-choice --tool-call-parser llama3_json`, or Llama answers in prose and
never calls a tool.[^llm]

# Budgets

| Limit | Setting | Default | On trip |
|---|---|---|---|
| Loop turns | `AGENT_MAX_TURNS` | 12 | `stop_reason: limit_turns` |
| Cumulative tokens | `AGENT_MAX_TOTAL_TOKENS` | 120000 | `stop_reason: limit_total_tokens` |
| Tool calls | `AGENT_MAX_TOOL_CALLS`, at most `AGENT_MAX_TURNS` - 1 | 11 | tools return a "budget exhausted" error, recorded as a refused call (a `budget_exhausted` warning) |
| Wall clock | `AGENT_TIMEOUT_S` | 300 | investigation recorded as failed |

Every tool result reports `tool_calls_left`, and the tool budget stops one turn before the turn limit,
so the model is told to answer while it still can instead of needing a follow-up call. A tripped turn or token limit becomes a `budget_exhausted` warning in [compliance](compliance-checks.md).

# Telemetry

Strands emits `invoke_agent relationship-investigator`, one `execute_event_loop_cycle` per turn, and
`chat` / `execute_tool` spans; Nexo sets `gen_ai.conversation.id` (the thread id) on all of them and
adds tool result counts. A failed tool call (an error returned to the model) has an error span with
`error.type` ([Observability](../operations/observability.md#failed-tool-calls)). Message content is
redacted unless capture is enabled ([Observability](../operations/observability.md)).

# Output

`AgentOutput`: `answer`, `citations` (ids parsed from `[chunk:<id>]`), `seed_chunk_ids`,
`fetched_chunk_ids`, `fetched_sensitivity`, `withheld_chunk_ids`, `tool_calls` (tool, ok, detail,
duration), `stop_reason`, `usage` and `error`.

A citation written without the `chk_` prefix counts only when it is the whole 16-hex hash
(`[chunk:714c2e550aacdb8f]`) and `chk_` plus that hash is a chunk id the run received. A shortened
hash, or one the run never received, stays uncited rather than guessed, so it is dropped without
raising `ungrounded_citations`; only `chk_`-prefixed ids reach that check.[^investigator]

[^investigator]: Investigator
[^llm]: build_strands_model
