---
type: Architecture Overview
title: System overview
description: What Nexo does and how its three modules fit together.
tags: [architecture, graph-rag, langgraph, strands, vllm, neo4j]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: readme
    resource: ../../README.md
    title: Project README
  - id: aws-health
    resource: ../references/aws-rag-healthcare.md
    title: AWS guidance, RAG for healthcare use cases
---

# Purpose

Nexo answers questions about how entities are connected (people, organizations, products, conditions,
medications, events) by tracing multi-hop paths in a knowledge graph and backing every claim with the
source text it came from. A human approves each answer before it is released.

# Shape

```
            ┌────────────── LangGraph StateGraph (checkpointed) ──────────────┐
request ──► │ router ──► investigator ─────────────► compliance ──interrupt──► │──► reviewer ──► answer
            │   │          │ @task: Strands Agent        (pure checks)       │
            │   ▼          │   ├─ query_knowledge_graph ──► Neo4j (READ tx)  │
            │  END         │   └─ fetch_vector_chunks  ──► chunk store       │
            └──────────────┴─────────────── vLLM (OpenAI API) ───────────────┘
ingest: document ─► heading-aware chunks ─► embeddings ─► chunk store
                                         └► vLLM triple extraction ─► evidence check ─► Neo4j
```

# Modules

| Module | Responsibility | Concepts |
|---|---|---|
| A. Storage | Turn documents into chunks, vectors and resolved graph triples that point back to their chunks. | [Ingestion pipeline](../components/ingestion-pipeline.md), [Relation ontology](../components/relation-ontology.md), [Entity resolution](../components/entity-resolution.md), [Graph store](../components/graph-store.md), [Chunk store](../components/chunk-store.md) |
| B. Micro-agent | Decide autonomously which relationships to trace and which evidence to read. | [Investigator agent](../components/investigator-agent.md), [query_knowledge_graph](../components/query-knowledge-graph.md), [find_paths and entity_neighbours](../components/graph-lookup-tools.md), [fetch_vector_chunks](../components/fetch-vector-chunks.md) |
| C. Orchestration | Enforce the fixed corporate workflow: validate, investigate, review. | [Orchestration graph](../components/orchestration-graph.md), [Compliance checks](../components/compliance-checks.md) |

Split of responsibility: the graph owns what must always happen (intake validation, audit trail, human
approval); the agent owns what cannot be scripted (which paths to follow, when evidence is enough). See
[ADR-0001](../decisions/adr-0001-strands-agent-inside-langgraph-task.md).

# Retrieval model

The router picks a strategy per request, following the multi-retriever pattern in the AWS healthcare RAG
guidance:[^aws-health]

- `graph`: the agent starts from the graph (named entities, paths, timelines).
- `vector`: the agent is seeded with semantic-search hits, then traces connections.
- `hybrid`: both; the default when unsure.

# Infrastructure

| Dependency | Role | Default endpoint |
|---|---|---|
| vLLM | Chat model for extraction, routing and the agent (Llama 3.3 70B) | `http://vllm-inference-service:8000/v1` |
| Embedding server | OpenAI-compatible embeddings (BAAI/bge-m3) | `http://vllm-embedding-service:8000/v1` |
| Neo4j | Knowledge graph | `neo4j://neo4j:7687` |
| Chunk store | Chunk text + vectors, looked up by `chunk_id` | `.data/vector_store.json` (local) |
| Checkpointer | Durable graph state across the review pause | SQLite or Postgres |
| OTEL collector | Trace and metric export ([Observability](../operations/observability.md)) | `OTEL_EXPORTER_OTLP_ENDPOINT` |

Full settings: [Configuration reference](../operations/configuration.md). How to run it all:
[Compose stack with vLLM](../operations/local-stack.md).[^readme]

[^readme]: Project README
[^aws-health]: AWS guidance, RAG for healthcare use cases
