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
find_paths(from_key: str, to_key: str, max_hops: int = 3, max_paths: int = 5) -> str  # JSON
entity_neighbours(entity_key: str, limit: int = 25) -> str  # JSON
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
an edge without `sensitivity_rank` counts as PHI. An unknown key, or an entity above clearance,
returns `unknown entity key`; an empty result hints at
[search_chunks](search-chunks.md). Budget, Neo4j errors and `chunk_ids_found` work as in
query_knowledge_graph.[^test-agent]

The MCP server's `nexo_neighbors` runs the same neighbour query and the same `visible_neighbours`
filter, and keeps its own result shape ([MCP server](mcp-server.md)).

[^tools]: Investigation tools
[^test-agent]: Tool tests
