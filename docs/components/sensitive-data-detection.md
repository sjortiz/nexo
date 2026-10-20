---
type: Component
title: Sensitive data detection
description: PII/PHI classification at ingestion, redaction policy, and caller clearance at query time.
resource: ../../src/nexo/storage/sensitive_data.py
tags: [module-a, security, pii, phi, compliance]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: sensitive
    resource: ../../src/nexo/storage/sensitive_data.py
    title: Detectors
  - id: comprehend
    resource: https://docs.aws.amazon.com/comprehend-medical/latest/dev/textanalysis-phi.html
    title: Amazon Comprehend Medical, detect PHI
  - id: aws-health
    resource: ../references/aws-rag-healthcare.md
    title: AWS guidance, RAG for healthcare use cases
---

# Sensitivity levels

Ordered `public` < `internal` < `pii` < `phi`. A chunk's level is the higher of the document's declared
level and anything detected. Entities and relationships take the level of their **least** sensitive
supporting chunk (`sensitivity_rank`): a caller sees them if any source they are cleared for supports
them, while a fact stated only in a PHI document stays PHI. Entities carry matching `:PII` / `:PHI`
labels; both are recomputed whenever a document is added, replaced or withdrawn.

# Providers (`SENSITIVE_DATA_PROVIDER`)

| Provider | Detects | Notes |
|---|---|---|
| `none` | nothing | Declared level only. |
| `regex` (default) | email, US SSN, Dominican cédula, phone, Luhn-valid payment card (PII); MRN (PHI) | High precision, low recall. A baseline, not a PHI control. |
| `comprehend_medical` | PHI entities from `DetectPHI` | Sends chunk text to an AWS regional endpoint; enable only where approved (BAA, region). Needs `AWS_REGION` and the `aws` extra.[^comprehend] |

# Policy (`SENSITIVE_DATA_POLICY`)

- `tag` (default): keep the text, raise the chunk's level.
- `redact`: replace detected spans with `[CATEGORY]` before embedding or extraction.

# Clearance at query time

`caller_clearance` (default `DEFAULT_CLEARANCE=internal`) is set by the authenticated service layer and
copied into the router's `ChunkFilter.max_sensitivity`; the router LLM cannot change it. It is enforced
in [fetch_vector_chunks](fetch-vector-chunks.md) (withheld chunks never reach the model),
[query_knowledge_graph](query-knowledge-graph.md) (redacts node maps above clearance) and, on
Enterprise, by database roles. [Compliance](compliance-checks.md) blocks approval of answers that rely
on PHI or contain detected identifiers until the reviewer acknowledges it. This follows the
healthcare guidance's emphasis on fine-grained access control and human review.[^aws-health]

## Read-time redaction

A chunk that is above the caller's clearance only because the detector found PII spans is shown with
those spans masked by type (`[EMAIL]`, `[PHONE]`), under the same chunk id; a caller at or above its
level gets the original. It applies to [fetch_vector_chunks](fetch-vector-chunks.md),
[search_chunks](search-chunks.md) and the seed passages, in `agents/redaction.py`, whichever chunk store
is used. Ingest keeps the text and the level; the detector runs again on the text at read time.

A chunk is withheld whole instead when:

- the document's declared level (`declared_sensitivity`) is above the caller's clearance, so the level
  was not just the detected spans;
- the detector finds a lower level than the chunk was stored at (or none), for example after switching
  provider;
- a span above clearance is PHI (MRN, Comprehend Medical entities): only `pii` spans are masked;
- the masked text still triggers the detector.

Chunks stored before `declared_sensitivity` existed have `None`; they are treated as classified by their
spans alone until re-ingested.

Search ranks the original text and its embedding (the store indexes them), so the redactor checks each
masked hit against its masked text. The query is masked first, so a whole detected value finds nothing.
A masked chunk is then returned only when at least one query word is a word of its masked text (the
placeholders' own words, such as `email`, do not count) and no query word is one that masking removed.
So fragments of a value (`0134`, `555-0134`, `415`, `jane`) do not find it, and neither does a query
that mixes them with other words. A chunk found only by its embedding is dropped, because the embedding
was computed from the original: masked chunks are found by their remaining words only. What this does
not prevent:

- ranking among the chunks that do match still uses the original text and embedding, so their order,
  and which of them fill the top k, can shift with how close the query is to a masked value;
- a word found both inside a masked value and elsewhere in the chunk (`beta` in `Beta Labs` and in
  `jane.roe@beta.example`) counts as a remaining word, and its masked occurrence still adds to the score;
- a word not in the chunk at all (a near miss such as `Jean`) passes the check when another query word
  matches, and only the embedding sees it.

The model-facing chunk, the ledger and spans never hold the masked-out text; the ledger keeps the stored
level, so [compliance](compliance-checks.md) still warns `pii_in_evidence`. A real detector adds a call
per over-clearance candidate at read time, which matters for `comprehend_medical`. `nexo mcp`'s
`nexo_search` and `nexo_get_chunks` ([MCP server](mcp-server.md#safeguards)) use the same redactor.
Graph rows are not masked: entities and edges above clearance stay redacted, and graph tools drop
chunk text from their rows ([read-only enforcement](graph-store.md#read-only-enforcement), ADR-0006).

See [ADR-0006](../decisions/adr-0006-sensitivity-and-caller-clearance.md).[^sensitive]

[^sensitive]: Detectors
[^comprehend]: Amazon Comprehend Medical, detect PHI
[^aws-health]: AWS guidance, RAG for healthcare use cases
