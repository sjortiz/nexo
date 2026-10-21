---
type: Agent Tool
title: find_paths and entity_neighbours
description: Fixed-query agent tools for paths between two entities and one entity's direct edges, so small models need no Cypher.
resource: ../../src/nexo/agents/tools.py
tags: [module-b, tool, neo4j]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T20:00:00Z }
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
find_paths(from_key: str, to_key: str, max_hops: int = 3, max_paths: int = 5) -> ToolResult
entity_neighbours(entity_key: str, limit: int = 25) -> ToolResult
# the model reads each result's content as JSON
```

# Behavior

Both run a fixed, parameterised query; the model supplies entity keys, not Cypher. Small local
models kept breaking these two patterns when writing them in
[query_knowledge_graph](query-knowledge-graph.md).[^tools]

- `find_paths`: up to `max_paths` (1-10) shortest paths of 1-`max_hops` (1-4) hops, in either
  direction, through `Entity` nodes only, so a shared chunk is never a link. Each hop gives
  `from`, `relation`, `to` and its `chunk_ids`.
- `entity_neighbours`: up to `limit` (1-100) direct edges with `outgoing`, `predicate`, `certainty`,
  `valid_at` and `chunk_ids`, best-supported first.

A path or edge touching anything above the caller's clearance is dropped and counted in `withheld`;
an edge without `sensitivity_rank` counts as PHI, and a visible edge's `evidence` is dropped unless the
chunk it was quoted from is within clearance ([graph store](graph-store.md#read-only-enforcement)).
`entity_neighbours` on an unknown key, or on an entity above clearance, returns `unknown entity key`.
`find_paths` returns that only when an endpoint key matches no entity; it does not check the endpoints'
clearance, so a path to an entity above clearance is withheld like any other and counted in `withheld`.
`nexo_find_paths` on `nexo mcp` is stricter: it checks both keys first and answers `unknown entity key`
for either case (porting that check to the agent is tracked in #81). An empty result hints at
[search_chunks](search-chunks.md), as "no visible path" when paths were withheld. Budget, Neo4j errors
and `chunk_ids_found` work as in query_knowledge_graph. An error (unknown key, same key twice, Neo4j failure) is a failed call with an
`error.type` on its span ([Observability](../operations/observability.md#failed-tool-calls)).[^test-agent]

The system prompt sends questions about one entity's roles, owners, suppliers and the like to
`entity_neighbours`, so the model does not guess relationship types in Cypher. `nexo mcp` serves the
same path query and filter as `nexo_find_paths` ([MCP server](mcp-server.md)).

The MCP server's `nexo_neighbors` runs the same neighbour query and the same `visible_neighbours`
filter, and keeps its own result shape; like here, an unknown key and an entity above clearance get
the same `unknown entity key` error ([MCP server](mcp-server.md)). Both read `predicate` (only on
`RELATED_TO` edges) and `valid_at` (only on dated edges) as `properties(r)['...']`, so Neo4j logs no
`01N52` "property key does not exist" warning on graphs without them.

[^tools]: Investigation tools
[^test-agent]: Tool tests
