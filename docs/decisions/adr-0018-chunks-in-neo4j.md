---
type: Architecture Decision
title: "ADR-0018: Chunk text and embeddings in Neo4j"
description: Chunks live on the graph's own (:Chunk) nodes with vector and full-text indexes, so every checkout of a stack sees the same chunks; review checkpoints move to a per-stack data directory.
tags: [adr, vector-store, neo4j, retrieval]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T21:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: store
    resource: ../../src/nexo/storage/neo4j_chunk_store.py
    title: Neo4jChunkStore
  - id: graph-store
    resource: ../../src/nexo/storage/graph_store.py
    title: GraphStore (replace_source)
  - id: adr-0004
    resource: adr-0004-chunk-store-interface-and-pgvector.md
    title: "ADR-0004: Chunk store behind an interface; pgvector for production"
  - id: adr-0017
    resource: adr-0017-secrets-from-a-provider.md
    title: "ADR-0017: Secrets from a provider, not a file you copy"
---

# Context

The stack is one per machine ([ADR-0017](adr-0017-secrets-from-a-provider.md)), but the chunk store was a
JSON file inside each checkout (`.data/vector_store.json`), and so were the review checkpoints. A second
checkout saw the shared graph with no chunks: every chunk the graph cited came back missing, and answers
were flagged as ungrounded. A question paused for review in one checkout could not be reviewed from
another.[^adr-0017]

# Decision

- **Chunks live on the graph's `(:Chunk)` nodes** (`CHUNK_STORE=neo4j`, the default): text, the
  embedded text and the embedding next to the metadata the graph already kept.[^store] One database,
  one lifecycle: graph and chunks cannot drift apart, deleting a document removes both, and a cluster
  needs no extra volume.
- **Search stays hybrid**: a Neo4j vector index (cosine) and a full-text index (accents folded), fused
  with Reciprocal Rank Fusion. Candidates are filtered with the same `ChunkFilter` as the local store, so
  clearance rules live in one place.
- **The graph swap keeps current chunks**: replacing a document deletes only its stale chunks and
  rewrites the rest's provenance, so their text survives re-ingestion.[^graph-store]
- **Review checkpoints** move to a per-stack data directory (`~/.local/share/nexo/<stack>/`), shared by
  every checkout.
- **Migration**: `nexo chunks import` copies an existing JSON store into Neo4j with its stored vectors (no
  re-embedding); `nexo chunks status` and `make check` report graph chunks without text.
- `CHUNK_STORE=local` keeps the in-process JSON store for offline runs and tests.

# Options considered

| Option | Against |
|---|---|
| The JSON store in a per-stack directory | Graph and chunk file can still drift (one restored or deleted without the other). |
| PostgreSQL + pgvector ([ADR-0004](adr-0004-chunk-store-interface-and-pgvector.md)) | A second database to run, back up and keep consistent with Neo4j.[^adr-0004] |
| Warn only | The second checkout stays broken. |

# Consequences

Supersedes ADR-0004's production target. Neo4j holds more data (text and one vector per chunk); the
vector index fetches extra candidates and filters them by clearance after the lookup. A different
embedding model needs the vector index recreated with its dimensions, and a re-ingest.

[^store]: Neo4jChunkStore
[^graph-store]: GraphStore (replace_source)
[^adr-0004]: "ADR-0004: Chunk store behind an interface; pgvector for production"
[^adr-0017]: "ADR-0017: Secrets from a provider, not a file you copy"
