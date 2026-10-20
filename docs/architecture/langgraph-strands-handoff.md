---
type: Integration Boundary
title: LangGraph to Strands handoff
description: The exact boundary where the LangGraph workflow yields control to the Strands agent, and how the agent drives vLLM.
resource: ../../src/nexo/orchestration/graph.py
tags: [architecture, langgraph, strands, vllm, handoff]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: graph
    resource: ../../src/nexo/orchestration/graph.py
    title: Orchestration graph (investigator node and task)
  - id: investigator
    resource: ../../src/nexo/agents/investigator.py
    title: Investigator (Strands agent)
  - id: test-agent
    resource: ../../tests/test_agent.py
    title: Strands loop test against a scripted vLLM client
---

# Where control changes hands

The `investigator` node calls the `strands_investigation` task and blocks on `.result()`. Inside the
task, one line hands control to Strands:[^graph]

```python
output = deps.investigator.investigate(user_query, request_id=request_id, scope=scope)
```

`Investigator.investigate` starts the agent loop:[^investigator]

```python
result = await asyncio.wait_for(
    agent.invoke_async(prompt, limits=Limits(turns=..., total_tokens=...)),
    timeout=settings.agent_timeout_s,
)
```

Control returns to LangGraph when the model stops calling tools, a `Limits` budget trips
(`stop_reason` `limit_turns` / `limit_total_tokens`) or the timeout fires.

# Why a `@task`

- **Checkpointed result.** The agent is expensive and non-deterministic. Its output is stored with the
  checkpoint, so resuming after the review pause never re-runs it.
- **Retry on transient failures.** `RetryPolicy(max_attempts=2, retry_on=is_transient)` re-runs the
  whole investigation when vLLM or Neo4j drops a connection. Safe because both tools are read-only.
- **Failure is data.** Any other exception becomes an `AgentOutput` with `error`, which
  [compliance](../components/compliance-checks.md) auto-rejects. The run still ends with an audit trail.

# How the agent crawls with vLLM

Each turn of the Strands event loop:

1. Sends the conversation and the two tool specs to vLLM's `/v1/chat/completions`.
2. vLLM's `llama3_json` tool-call parser turns Llama's output into OpenAI `tool_calls`.
3. Strands runs the requested tools and appends their results to the conversation.

The model typically alternates:

- [query_knowledge_graph](../components/query-knowledge-graph.md): read-only Cypher; returns rows plus
  every `chunk_id` it saw.
- [find_paths and entity_neighbours](../components/graph-lookup-tools.md): fixed path and
  neighbour lookups by entity key.
- [fetch_vector_chunks](../components/fetch-vector-chunks.md): the exact text for those ids; this is the
  evidence it must cite.

Nothing scripts the order or depth. The system prompt states a goal and how to judge evidence, not
steps. This exact sequence (graph query, chunk fetch, cited answer) is exercised against a scripted
OpenAI-compatible client in the test suite.[^test-agent]

# Tracing across the boundary

The investigator node runs inside its own current span. LangGraph runs tasks with the caller's
`contextvars`, and `asyncio.run` copies them again, so Strands' `invoke_agent relationship-investigator`
span (and every cycle, model and tool span under it) is a child of `langgraph.node investigator`
inside the `invoke_workflow` trace. Nexo does not add its own agent span. See
[Observability](../operations/observability.md).

[^graph]: Orchestration graph (investigator node and task)
[^investigator]: Investigator (Strands agent)
[^test-agent]: Strands loop test against a scripted vLLM client
