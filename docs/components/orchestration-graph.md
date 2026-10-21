---
type: State Machine
title: Orchestration graph
description: The 3-node LangGraph state machine - router/validator, investigator and compliance with human-in-the-loop.
resource: ../../src/nexo/orchestration/graph.py
tags: [module-c, langgraph, workflow, human-in-the-loop]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: graph
    resource: ../../src/nexo/orchestration/graph.py
    title: build_graph
  - id: state
    resource: ../../src/nexo/orchestration/state.py
    title: GraphState, RouteDecision, ReviewDecision, ReviewPacket
  - id: test-graph
    resource: ../../tests/test_graph.py
    title: Orchestration tests
---

# Topology

Generated from the compiled graph (`nexo graph`); a test fails if this copy drifts from the code.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	router(router)
	investigator(investigator)
	compliance(compliance)
	__end__([<p>__end__</p>]):::last
	__start__ --> router;
	investigator --> compliance;
	router -. &nbsp;rejected&nbsp; .-> __end__;
	router -.-> investigator;
	compliance --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

The edges are fixed; only the router's conditional edge (dotted) branches: `rejected` ends the run,
anything else goes to the investigator. The compliance node pauses with `interrupt()` for human
approval unless the [review policy](../operations/human-review.md#review-policy) approves the answer;
under the default, `on-findings`, an answer with any finding pauses. State keys are listed in
[Request lifecycle](../architecture/request-lifecycle.md#state).[^state]

# Nodes

**router** (validator)

- Deterministic gate first: empty query, or longer than `MAX_QUERY_CHARS` (2000) → `rejected`.
- Then the router LLM returns a `RouteDecision`: `target`, `reason`, `strategy`
  (`graph`/`vector`/`hybrid`), optional `doc_types`, `effective_from`, `effective_to`.
- Out of scope: writes, bulk export of personal records, prompt extraction, unrelated tasks.
- Fails closed: if the router LLM is unreachable or returns invalid output twice, the request is
  `rejected` with `router unavailable (...)`. The agent is never called with an unvalidated request.
- The scope's `max_sensitivity` is the caller's clearance, not a model output.

**investigator** — runs the [Strands agent](investigator-agent.md) in a checkpointed `@task`; see
[the handoff](../architecture/langgraph-strands-handoff.md).

**compliance** — runs [compliance checks](compliance-checks.md). Unless the
[review policy](../operations/human-review.md#review-policy) approves the answer as `system:auto`, it
then pauses with `interrupt()`, passing a `ReviewPacket` as JSON:[^state]

```json
{"request_id": "...", "user_query": "...", "draft_answer": "...", "citations": ["chk_..."],
 "compliance_report": {"findings": [...], "blocking": false},
 "resume_with": {"...ReviewDecision JSON schema..."}}
```

Resume value (`ReviewDecision`):

```json
{"approval_status": "approved", "reviewer": "analyst@corp", "comment": "ok", "acknowledged_findings": false}
```

Invalid input, or approving blocking findings without `acknowledged_findings: true`, calls
`interrupt()` again with an `error` key. Everything before `interrupt()` is pure, because LangGraph
re-executes the node from the top on resume.[^graph]

# Guarantees (tested)

Resume does not re-run the agent; rejected requests never reach it; the router cannot raise clearance;
a transient vLLM error retries the investigation once; a review survives a process restart with the
SQLite checkpointer; one trace covers request, nodes and the agent.[^test-graph]

[^graph]: build_graph
[^state]: GraphState, RouteDecision, ReviewDecision, ReviewPacket
[^test-graph]: Orchestration tests
