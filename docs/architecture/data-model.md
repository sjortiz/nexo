---
type: Graph Schema
title: Knowledge graph data model
description: Neo4j lexical and domain graph - labels, relationships, properties - and the chunk_id link back to the vector store.
resource: ../../src/nexo/storage/graph_store.py
tags: [neo4j, schema, provenance]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: graph-store
    resource: ../../src/nexo/storage/graph_store.py
    title: Graph store (Cypher)
  - id: schemas
    resource: ../../src/nexo/storage/schemas.py
    title: Pydantic schemas
  - id: it
    resource: ../../tests/test_neo4j_integration.py
    title: Neo4j integration tests
---

# Schema

```
(:Document)-[:FIRST_CHUNK]->(:Chunk)-[:NEXT_CHUNK]->(:Chunk)-[:PART_OF]->(:Document)   lexical graph
(:Entity:<EntityType>[:PII][:PHI])-[:<RELATION>]->(:Entity)                          domain graph
(:Entity)-[:MENTIONED_IN {surface, resolution}]->(:Chunk)                            provenance
```

**`:Document`** — `source_id` (unique), `title`, `doc_type`, `source_uri`, `effective_date`,
`sensitivity`.

**`:Chunk`** — `chunk_id` (unique), `source_id`, `ordinal`, `section_path`, `sensitivity`. Every chunk
of a document is written, including chunks with no extracted facts.

**`:Entity`** (plus one type label and optional sensitivity labels)

| Property | Type | Description |
|---|---|---|
| `key` | string, unique | `<Type>:<normalized name>` (case, punctuation, legal suffixes, honorifics folded); see [entity resolution](../components/entity-resolution.md). |
| `name` | string | Canonical name as first extracted. |
| `aliases` | list | Other surface forms, derived from `MENTIONED_IN.surface` (sorted). |
| `type` | string | One of the closed [entity types](#entity-types). |
| `description` | string | First non-empty description seen. |
| `chunk_id` | string | Chunk where the entity was first extracted. |
| `chunk_ids` | list | Every chunk that mentions it, across documents. |
| `source_ids` | list | Every document that mentions it. |
| `sensitivity_rank` | int | Least sensitive chunk that mentions it, as `Sensitivity.rank` (0 public, 1 internal, 2 pii, 3 phi; an unknown level counts as phi): visible to a caller cleared for any one of its sources. `:PII`/`:PHI` labels follow it. |
| `proposed_codes_json` | string | Terminology codes printed in the source (ICD10CM, RXNORM, SNOMEDCT, MESH, UMLS); unverified. |

**`-[:MENTIONED_IN]->`** — `surface` (the name as written) and `resolution` (`exact`, `alias`,
`llm`, `new`, `stated_alias`): how that mention was mapped to the entity. See
[Entity resolution](../components/entity-resolution.md).

**`-[:<RELATION>]->`** — canonical relation from the [relation ontology](../components/relation-ontology.md),
or the normalized predicate when no ontology is configured. Out-of-ontology predicates are
`RELATED_TO` edges keyed by `predicate`.

| Property | Description |
|---|---|
| `chunk_id`, `chunk_ids` | First-seen and all supporting chunks. |
| `evidence` | Verbatim quote that states the fact. |
| `certainty` | `asserted`, `possible` or `negated`. |
| `valid_at` | ISO date the fact holds, when stated. |
| `extracted_as` | Every extracted predicate that produced this edge (e.g. `BOUGHT`, `ACQUIRED_BY`). |
| `predicate` | `RELATED_TO` only: the original predicate. |
| `sensitivity_rank` | Least sensitive chunk in `chunk_ids`: a fact stated only in a PHI document stays PHI even between visible entities. |

# Entity types

`Person`, `Organization`, `Location`, `Product`, `Event`, `Concept`, `Condition`, `Medication`,
`Procedure`. The set is closed because the values become Neo4j labels, which cannot be query
parameters.[^schemas]

# The chunk_id contract

Every node and edge carries the chunk(s) it came from, and every such `chunk_id` resolves in the
[chunk store](../components/chunk-store.md). Ingestion preserves this through ordering: upsert new
chunks, then swap the document's subgraph in one transaction, then delete superseded chunks. See
[Ingestion pipeline](../components/ingestion-pipeline.md).

# Lifecycle

`replace_source(document, chunks, triples)` runs in one write transaction:[^graph-store]

1. Removes the document's chunk ids from every `chunk_ids` / `source_ids` list.
2. Deletes edges, chunks, the document node and entities that only this document supported.
3. Re-points `chunk_id` to a surviving chunk where the first-seen chunk is gone.
4. Writes the document, its chunks and their order, then upserts the new triples and mentions.
5. Recomputes `aliases`, `sensitivity_rank` and the `:PII`/`:PHI` labels for every entity it touched
   and their edges, so a withdrawn document takes its aliases, merges and sensitivity with it (ranks go
   down as well as up).

Cross-document merging, label assignment, chunk ordering, alias withdrawal and withdrawal
cleanup are verified against Neo4j 5.26.[^it]

# Constraints and indexes

- `entity_key`: `Entity.key` unique
- `chunk_id`: `Chunk.chunk_id` unique
- `document_source_id`: `Document.source_id` unique
- `entity_chunk_id`, `entity_type`: range indexes
- `entity_name_text`: text index on `Entity.name` (serves `CONTAINS` in agent queries and resolution
  blocking)

[^graph-store]: Graph store (Cypher)
[^schemas]: Pydantic schemas
[^it]: Neo4j integration tests
