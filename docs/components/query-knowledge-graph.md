---
type: Agent Tool
title: query_knowledge_graph
description: Agent tool that runs one read-only Cypher query against Neo4j and returns rows plus every chunk_id seen.
resource: ../../src/nexo/agents/tools.py
tags: [module-b, tool, neo4j, cypher]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Investigation tools
  - id: test-agent
    resource: ../../tests/test_agent.py
    title: Tool tests
---

# Signature

```python
query_knowledge_graph(cypher_query: str) -> ToolResult  # the model reads its content as JSON
```

# Behavior

1. Reserves one call from the investigation's tool budget.
2. Validates the query with the [read-only guard](graph-store.md#read-only-enforcement).
3. Runs it through `GraphStore.run_read_query` (READ transaction, timeout, row cap).
4. Redacts any node or relationship map whose `sensitivity_rank` is above the caller's clearance.
5. Collects every value matching `chk_[0-9a-f]{16}` anywhere in the rows into `chunk_ids_found`.
6. Serializes within `TOOL_RESULT_MAX_CHARS` (12000), dropping trailing rows (counted in
   `omitted_for_size`) rather than cutting JSON.[^tools]

# Examples

Success:

```json
{"row_count": 1, "truncated": false,
 "rows": [{"path": ["Acme Holdings", "Beta Labs", "Gamma Clinics"], "chunk_ids": ["chk_3f1a..."]}],
 "chunk_ids_found": ["chk_3f1a..."]}
```

Errors are returned to the model as data so it can correct itself. The call is recorded as failed
and its `execute_tool` span is marked failed with an `error.type`
([Observability](../operations/observability.md#failed-tool-calls)):

| Cause | Result |
|---|---|
| Write or procedure call | `{"error": "read-only access: DETACH is not allowed", "hint": "rewrite as a single read-only query"}` |
| Syntax error, timeout, access-mode violation | `{"error": "<Neo4j message>", "code": "Neo.ClientError..."}` |
| Neo4j unreachable after retries | `{"error": "knowledge graph unavailable; do not retry, report what you have"}` |
| Budget used up | `{"error": "tool budget exhausted; write your answer from the evidence gathered"}` |
| Too large even with every row dropped | `{"error": "result too large; return fewer or smaller properties"}` |

# Limits

Redaction only sees values projected as whole nodes or relationships. `RETURN e.name` on a PHI entity
is a scalar the tool cannot classify; database roles close that gap (see
[ADR-0006](../decisions/adr-0006-sensitivity-and-caller-clearance.md)). Redacted nodes' chunk ids are
not leaked into `chunk_ids_found`.[^test-agent]

[^tools]: Investigation tools
[^test-agent]: Tool tests
