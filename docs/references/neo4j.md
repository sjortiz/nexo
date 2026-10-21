---
type: Reference
title: Neo4j
description: The graph database holding Nexo's lexical and domain graph, and the driver and Cypher features Nexo relies on.
resource: https://github.com/neo4j/neo4j-python-driver
tags: [reference, core-stack, neo4j, graph]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: driver
    resource: https://neo4j.com/docs/python-manual/current/
    title: Neo4j Python driver manual
  - id: indexes
    resource: https://neo4j.com/docs/cypher-manual/current/indexes/search-performance-indexes/overview/
    title: Neo4j search-performance indexes (range, text)
  - id: collect
    resource: https://neo4j.com/docs/cypher-manual/current/subqueries/collect/
    title: Neo4j COLLECT subqueries
  - id: code
    resource: ../../src/nexo/storage/graph_store.py
    title: Nexo graph store
---

# Role in Nexo

Neo4j stores the graph: `Document`/`Chunk` nodes (lexical graph) and resolved entities with
evidence-carrying relationships (domain graph), each pointing back to its chunks. The agent queries it
with read-only Cypher. Server: Neo4j 5.26 Community in compose; the test suite also passes on 2026.09 and 2026.10, where
chunk search uses the `SEARCH` clause ([Chunk store](../components/chunk-store.md#neo4j-versions)).
Driver pinned as `neo4j>=6.4,<7`. Schema: [Knowledge graph data model](../architecture/data-model.md).

# Features used

| Feature | Where | Why |
|---|---|---|
| Bolt driver, `execute_write` / `execute_read`, `unit_of_work(timeout=...)` | `GraphStore`[^code] | Managed transactions with driver retries; per-query timeout for agent Cypher.[^driver] |
| `READ_ACCESS` sessions | `run_read_query` | The server refuses writes even if the Cypher guard is bypassed ([ADR-0002](../decisions/adr-0002-read-only-defense-in-depth.md)). |
| `UNWIND` + `MERGE` batches, one transaction per document | `replace_source` | Atomic replace-on-reingest of a document's subgraph. |
| `COLLECT { ... ORDER BY }` subqueries | `_refresh_aliases` | Deterministic, sorted entity aliases.[^collect] |
| Uniqueness constraints, range indexes, a text index on `Entity.name` | `ensure_schema` | Entity keys, chunk ids, and `CONTAINS` lookups for agents and resolution blocking.[^indexes] |
| Labels for sensitivity (`:PII`, `:PHI`) | entity upsert | Enterprise role rules can hide them ([ADR-0006](../decisions/adr-0006-sensitivity-and-caller-clearance.md)). |

[^driver]: Neo4j Python driver manual
[^indexes]: Neo4j search-performance indexes (range, text)
[^collect]: Neo4j COLLECT subqueries
[^code]: Nexo graph store
