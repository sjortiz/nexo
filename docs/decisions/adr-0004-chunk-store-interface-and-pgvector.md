---
type: Architecture Decision
title: "ADR-0004: Chunk store behind an interface; pgvector for production"
description: Chunk text and vectors live outside Neo4j behind ChunkStore; a local store ships now and Aurora/RDS pgvector is the production target.
tags: [adr, vector-store, pgvector, opensearch]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: vector-store
    resource: ../../src/nexo/storage/vector_store.py
    title: ChunkStore
  - id: aws-vdb
    resource: ../references/aws-vector-database-choice.md
    title: AWS guidance, choosing a vector database for RAG
---

# Context

The agent's main hop is graph → `chunk_id` → exact text, so the store needs a reliable get-by-id,
metadata filters and read-after-write consistency. Similarity search is secondary (seeding).

# Decision

Depend on the `ChunkStore` protocol.[^vector-store] Ship `LocalChunkStore` for single-node use and tests.
For production, implement it on PostgreSQL with pgvector (self-hosted or any managed Postgres); use OpenSearch if hybrid
lexical + vector search or very high query rates become requirements.

# Options considered

Per the AWS vector database guidance:[^aws-vdb]

| Option | Fit |
|---|---|
| PostgreSQL + pgvector (incl. managed, e.g. Aurora/RDS) | Best: primary-key lookup, SQL filters, ACID. |
| OpenSearch (incl. managed) | Good for hybrid search and high QPS; eventual consistency after writes. |
| Neptune Analytics | Only if Neo4j were replaced with one combined graph + vector store. |
| Bedrock Knowledge Bases | Managed pipeline; poor fit when the pipeline and LLM are self-hosted. |
| Neo4j vector index | Fewer systems; couples chunk storage to the graph's scaling and backup. |

# Consequences

Two stores must stay consistent; ingestion order (vectors, graph, cleanup) handles it. Swapping
backends touches one class.

[^vector-store]: ChunkStore
[^aws-vdb]: AWS guidance, choosing a vector database for RAG
