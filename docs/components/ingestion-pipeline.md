---
type: Pipeline
title: Ingestion pipeline
description: Document to chunks, vectors and graph triples, keeping every chunk_id resolvable.
resource: ../../src/nexo/storage/ingest.py
tags: [module-a, ingestion, neo4j, embeddings]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: ingest
    resource: ../../src/nexo/storage/ingest.py
    title: KnowledgeGraphIngestor
  - id: test-ingest
    resource: ../../tests/test_ingest.py
    title: Ingestion tests
---

# Stages

For one document, `KnowledgeGraphIngestor.ingest(text, metadata)`:[^ingest]

1. **Lint and flatten.** Report authoring problems; rewrite Markdown tables as bullets. See
   [chunking](chunking.md).
2. **Chunk.** Heading-aware split, then size-bounded split (`CHUNK_SIZE` 1200, `CHUNK_OVERLAP` 150).
   Chunk ids are deterministic: `chk_` + 16 hex of sha256(source_id, ordinal, text).
3. **Classify.** Each chunk's sensitivity is the higher of the declared document level and anything
   the [detector](sensitive-data-detection.md) finds. With `SENSITIVE_DATA_POLICY=redact`, detected
   values are replaced (`[EMAIL]`) before anything is embedded or extracted.
4. **Upsert vectors.** Embedded text = title > section path + chunk text + glossary notes.
5. **Extract triples.** One LLM call per chunk, `EXTRACTION_CONCURRENCY` (8) in parallel, so vLLM's
   continuous batching turns parallelism into throughput. The tail of the previous chunk
   (`EXTRACTION_CONTEXT_CHARS`, 500) is shown as context so references resolve across chunk
   boundaries. See [triple extraction](triple-extraction.md).
6. **Filter and map.** Drop triples whose evidence is not in the chunk or that loop back to the same
   entity; map predicates onto the [relation ontology](relation-ontology.md).
7. **Resolve entities** against each other and the existing graph ([entity resolution](entity-resolution.md)).
8. **Replace subgraph.** `GraphStore.replace_source` writes the document, all chunks, triples and
   mentions in one Neo4j transaction.
9. **Delete stale vectors.** Chunks of this document that are not in the new version.

`ingest()` needs both a chunk store and a graph writer in its `IngestionTargets` and raises
`ValueError` without them. `extract_triples(text, metadata)` runs steps 1–3, 5 and 6 only, with no
writes and no resolution, so its targets may be None; `nexo eval-extraction` uses it.

Steps 4 → 8 → 9 keep the [chunk_id contract](../architecture/data-model.md#the-chunk_id-contract):
Neo4j never references a chunk the store cannot return, even if the process dies mid-way.

# Report

```json
{
  "source_id": "deals", "chunk_count": 12, "triples_extracted": 41, "triples_written": 37,
  "dropped": {"evidence_not_in_chunk": 3, "self_loop_after_resolution": 1},
  "relations": {"canonical": 30, "remapped": 5, "fallback": 2},
  "resolution": {"new": 20, "exact": 9, "alias": 4, "llm": 2},
  "failed_chunk_ids": [], "sensitivity": {"internal": 11, "pii": 1},
  "stale_chunks_deleted": 0, "lint_warnings": ["1 table(s) flattened to bullets; ..."]
}
```

A chunk whose extraction fails (invalid JSON after retries, or vLLM unreachable) is listed in
`failed_chunk_ids`; its text stays in the chunk store and its `Chunk` node in the graph, and re-running
the ingest retries it. The CLI logs a warning and exits non-zero when any chunk failed.

# Examples

```bash
nexo ingest docs/memo.md --source-id memo-2025-06 --doc-type memo \
  --effective-date 2025-06-30 --sensitivity internal
```

Retry-on-validation-error, evidence dropping, previous-chunk context, ontology mapping, resolution,
PII classification, failed-chunk reporting, re-ingestion, redaction and the missing-targets error are each covered by tests.[^test-ingest]

[^ingest]: KnowledgeGraphIngestor
[^test-ingest]: Ingestion tests
