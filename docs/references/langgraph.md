---
type: Reference
title: LangGraph
description: The orchestration framework behind Nexo's 3-node workflow - StateGraph, checkpointed tasks, interrupts and durable checkpointers - and how Nexo uses each.
resource: https://github.com/langchain-ai/langgraph
tags: [reference, core-stack, orchestration, langgraph]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: overview
    resource: https://docs.langchain.com/oss/python/langgraph/overview
    title: LangGraph overview
  - id: interrupts
    resource: https://docs.langchain.com/oss/python/langgraph/interrupts
    title: LangGraph interrupts (human-in-the-loop)
  - id: persistence
    resource: https://docs.langchain.com/oss/python/langgraph/persistence
    title: LangGraph persistence
  - id: functional
    resource: https://docs.langchain.com/oss/python/langgraph/functional-api
    title: LangGraph functional API (tasks)
  - id: code
    resource: ../../src/nexo/orchestration/graph.py
    title: Nexo orchestration graph
---

# Role in Nexo

LangGraph runs the fixed workflow: router → investigator → compliance, with a human approval pause. It
owns what must always happen in order; the open-ended reasoning happens inside the
[Strands agent](strands-agents.md).[^overview] Pinned as `langgraph>=1.2,<2`. Diagram and node details:
[Orchestration graph](../components/orchestration-graph.md).

# Features used

| Feature | Where | Why |
|---|---|---|
| `StateGraph` with a `TypedDict` state, conditional edges | `build_graph`[^code] | A rigid, inspectable state machine; the router's edge is the only branch. |
| `Annotated[list, operator.add]` reducer | `GraphState.agent_outputs` | Append-only audit trail across retries and resumes. |
| `@task` with `RetryPolicy` | `strands_investigation` | Checkpoints the agent's result so resuming never re-runs it; retries transient vLLM/Neo4j failures.[^functional] |
| `interrupt()` / `Command(resume=...)` | compliance node, `Runtime.review` | Pauses for a human decision and validates the resume value, re-prompting on bad input.[^interrupts] |
| Checkpointers: `InMemorySaver`, `SqliteSaver`, `PostgresSaver` | `open_checkpointer` | Reviews outlive the process; SQLite for one node, Postgres for replicas.[^persistence] |
| `stream(..., stream_mode="updates")` | `Runtime._drive` | Each state transition becomes a span event. |
| `get_graph().draw_mermaid()` | `workflow_mermaid`, `nexo graph` | The published diagram is generated from the compiled graph. |

Decisions: [ADR-0001](../decisions/adr-0001-strands-agent-inside-langgraph-task.md) (agent inside a task),
[ADR-0003](../decisions/adr-0003-durable-checkpoints-for-review.md) (durable checkpoints).

# Not used

LangGraph Platform / Agent Server and LangGraph Studio: `Runtime` and the CLI drive the graph directly
(see [ADR-0011](../decisions/adr-0011-langsmith-adoption.md)).

[^overview]: LangGraph overview
[^interrupts]: LangGraph interrupts (human-in-the-loop)
[^persistence]: LangGraph persistence
[^functional]: LangGraph functional API (tasks)
[^code]: Nexo orchestration graph
