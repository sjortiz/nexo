---
type: Interface
title: Chunk store
description: Get-by-id, filtered hybrid search (BM25 + vector) and delete-by-document for chunk text and vectors.
resource: ../../src/nexo/storage/vector_store.py
tags: [module-a, vector-store, embeddings]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: vector-store
    resource: ../../src/nexo/storage/vector_store.py
    title: ChunkStore and LocalChunkStore
  - id: neo4j-store
    resource: ../../src/nexo/storage/neo4j_chunk_store.py
    title: Neo4jChunkStore
  - id: aws-vdb
    resource: ../references/aws-vector-database-choice.md
    title: AWS guidance, choosing a vector database for RAG
---

# Contract

```python
class ChunkStore(Protocol):
    def upsert(self, chunks, embedding_texts) -> None: ...
    def get_by_ids(self, chunk_ids) -> list[ChunkRecord]: ...       # graph -> text hop
    def search(self, query, k, chunk_filter) -> list[ChunkRecord]: ...  # seeding (hybrid by default)
    def delete_source(self, source_id, keep=frozenset()) -> int: ...  # re-ingestion
```

`get_by_ids` returns records in request order and silently skips unknown ids.[^vector-store]

# Schema

`ChunkRecord`:

| Field | Description |
|---|---|
| `chunk_id` | `chk_` + 16 hex; deterministic. |
| `source_id`, `ordinal` | Document and position. |
| `text` | Exact chunk text (evidence). |
| `section_path`, `title` | Heading path and document title. |
| `doc_type`, `source_uri`, `effective_date` | Document metadata, filterable. |
| `sensitivity` | `public`, `internal`, `pii` or `phi`. |
| `ingested_at`, `schema_version` | Bookkeeping. |

`ChunkFilter` (`doc_types`, `source_ids`, `effective_from`, `effective_to`, `max_sensitivity`) is
applied before ranking in `search` and again when the agent fetches chunks. Undated chunks fail any
date-bounded filter.

# Search

`RETRIEVAL_MODE` selects the ranking (see [configuration](../operations/configuration.md)):

| Mode | Ranking | Good at |
|---|---|---|
| `hybrid` (default) | BM25 and vector rankings fused with Reciprocal Rank Fusion (k = 60) | Both: exact terms first, paraphrases still found |
| `keyword` | BM25 only; chunks with no query term are never returned | Names, product codes, IDs, rare terms |
| `vector` | Embedding similarity only | Paraphrases and cross-language matches |

BM25 runs in-process over the embedded text (title, section path, chunk, glossary notes), with
case- and accent-folded tokens so "compró" matches "compro". The index rebuilds lazily after any
write. Before fusion each ranking keeps `candidate_count(k, mode)` allowed hits (`k` in `vector`
mode, else `max(4k, 20)`), the same in both stores. Each search is a `retrieval nexo-chunks` span
with `nexo.retrieval.mode`.

# Implementations

| Backend | Status | Use |
|---|---|---|
| `Neo4jChunkStore` | default (`CHUNK_STORE=neo4j`) | Text, embedded text and embedding on the graph's `(:Chunk)` nodes; a vector index (cosine) and a full-text index (accents folded), fused with RRF; filtered with the same `ChunkFilter`. Shared by every checkout of a stack; `nexo chunks import` copies a JSON store in.[^neo4j-store] |
| `LocalChunkStore` | `CHUNK_STORE=local` | Wraps LangChain `InMemoryVectorStore`; persisted to `VECTOR_STORE_PATH` with an atomic file replace. Offline runs and tests. |
| PostgreSQL + pgvector (self-hosted or managed) | superseded | Was the production target; a second database to keep consistent with Neo4j. |
| OpenSearch (self-hosted or managed) | alternative | Very high query rates; native BM25 + vector; eventual consistency after writes. |

The recommendation follows the AWS vector database guidance's decision criteria.[^aws-vdb] See
[ADR-0018](../decisions/adr-0018-chunks-in-neo4j.md), which supersedes
[ADR-0004](../decisions/adr-0004-chunk-store-interface-and-pgvector.md).

# Neo4j versions

`Neo4jChunkStore` reads its two indexes in whichever form the server supports, decided once per
store from `CALL dbms.components()`:[^neo4j-store]

| Neo4j | Index lookup |
|---|---|
| 2026.09 and later | `CYPHER 25 ... SEARCH c IN (VECTOR INDEX ... )` and `(FULLTEXT INDEX ...)`: no deprecation notifications. |
| 5.26 (compose and CI default) up to 2026.08 | `db.index.vector.queryNodes` and `db.index.fulltext.queryNodes`; 2026.x deprecates them. |

`SEARCH` reads vector indexes from 2026.01 and full-text indexes from 2026.09, so the store switches
both at 2026.09. Both forms ask the index for the same number of hits and rank them the same; the Neo4j integration
tests compare them on each server (`make test-neo4j NEO4J_TEST_IMAGE=neo4j:2026.10-community`).

[^vector-store]: ChunkStore and LocalChunkStore
[^neo4j-store]: Neo4jChunkStore
[^aws-vdb]: AWS guidance, choosing a vector database for RAG
