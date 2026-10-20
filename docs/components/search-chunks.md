---
type: Agent Tool
title: search_chunks
description: Agent tool that searches the source text, so the agent can check the documents when the graph has no entity or path.
resource: ../../src/nexo/agents/tools.py
tags: [module-b, tool, vector-store, evidence]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T19:40:00Z }
sources:
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Investigation tools
---

# Signature

```python
search_chunks(query: str, k: int = 5) -> str  # JSON
```

# Behavior

1. Reserves one call from the tool budget.
2. Calls `ChunkStore.search` (hybrid BM25 + vector by default; see [chunk store](chunk-store.md)) with the
   request's scope and a few times `k` candidates, then drops chunks outside the scope or above clearance.
   A chunk above clearance only because of detected PII spans stays, masked
   ([read-time redaction](sensitive-data-detection.md#read-time-redaction)); the query is masked first, and
   ranking uses the original text. `k` is capped at `AGENT_MAX_CHUNKS_PER_CALL`.
3. Returns the chunks in the same shape as [fetch_vector_chunks](fetch-vector-chunks.md) and records them
   in the evidence ledger, so the answer may cite them and [compliance](compliance-checks.md) treats them
   as grounded.[^tools]

# Why it exists

Extraction can miss an entity or relation that the text states. The router also sends questions that
name entities to the `graph` strategy, which seeds no passages. With only graph queries and fetch by id,
the agent could not reach that text and reported the entity as absent. The prompt now says absence from
the graph is not evidence of absence and to search the text before concluding.

[^tools]: Investigation tools
