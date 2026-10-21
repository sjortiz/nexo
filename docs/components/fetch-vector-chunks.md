---
type: Agent Tool
title: fetch_vector_chunks
description: Agent tool that returns the exact source text for chunk ids found in the graph, enforcing clearance and scope.
resource: ../../src/nexo/agents/tools.py
tags: [module-b, tool, vector-store, evidence]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Investigation tools
---

# Signature

```python
fetch_vector_chunks(chunk_ids: list[str]) -> ToolResult  # the model reads its content as JSON
```

# Behavior

1. Reserves one call from the tool budget.
2. Dedupes ids, separates malformed ones (`invalid_ids`) and caps the batch at
   `AGENT_MAX_CHUNKS_PER_CALL` (10); the rest are listed in `over_limit_not_fetched`.
3. Loads records with `ChunkStore.get_by_ids`.
4. Withholds any chunk outside the router's doc-type/date scope or above clearance, except that a chunk
   above clearance only because of detected PII spans is returned with them masked, such as
   `[EMAIL]` ([read-time redaction](sensitive-data-detection.md#read-time-redaction)). Withheld text never
   reaches the model.
5. Records only chunks the model actually received in the evidence ledger (`fetched_sensitivity`).
   [Compliance](compliance-checks.md) treats any cited id outside that ledger as ungrounded.[^tools]

# Examples

```json
{"chunks": [{"chunk_id": "chk_3f1a...", "source": "Deals memo > Deals",
             "effective_date": "2025-06-30", "text": "Acme Holdings acquired Beta Labs in 2024."}],
 "withheld": ["chk_9c0e..."], "not_found": [], "invalid_ids": ["acme"], "over_limit_not_fetched": []}
```

If the chunk store fails, the tool returns `{"error": "chunk store unavailable; report what you have"}`
and records the exception on its span. Chunks dropped to fit `TOOL_RESULT_MAX_CHARS` are counted in
`omitted_for_size` and are not evidence: only chunks the model received are recorded as fetched.

[^tools]: Investigation tools
