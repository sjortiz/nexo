---
type: Architecture Decision
title: "ADR-0006: Sensitivity classification and caller clearance"
description: Chunks and entities carry a sensitivity level; the caller's clearance, not the model, decides what evidence the agent can see.
tags: [adr, security, pii, phi]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Clearance enforcement in tools
  - id: aws-health
    resource: ../references/aws-rag-healthcare.md
    title: AWS guidance, RAG for healthcare use cases
---

# Context

The corpus can contain PII and PHI. The healthcare RAG guidance calls for fine-grained access control
and human review.[^aws-health] The router is an LLM and can be manipulated, so it must not decide
access.

# Decision

- Classify each chunk at ingestion (declared level, raised by detection). Entities and relationships
  take the level of their least sensitive supporting chunk (`sensitivity_rank`, recomputed on every
  change), and entities carry matching `:PII` / `:PHI` labels. Taking the most sensitive source instead
  hid an organization from internal callers as soon as one PHI note mentioned it, and never let a level
  go back down.
- Take `caller_clearance` from the authenticated caller and copy it into the investigation scope after
  routing.
- Enforce it where data leaves a store: chunk text is withheld above clearance, node maps are redacted,
  and Enterprise roles hide labeled nodes.[^tools]
- Make PHI reliance and identifiers in answers blocking findings at review.
- **Amended 2026-10-20:** chunk text above clearance is masked instead of withheld when the only reason
  is detected PII spans; chunks carry `declared_sensitivity` so a declared level is never
  mistaken for a detected one. Everything else stays withheld.
  See [read-time redaction](../components/sensitive-data-detection.md#read-time-redaction).
- **Amended 2026-10-20 (MCP):** `nexo mcp`'s chunk tools (`nexo_search`, `nexo_get_chunks`) use the same
  redactor as the agent, with the server's clearance. Graph rows (entities, edges, Cypher results) above
  clearance stay redacted wholesale, even when every supporting chunk is `pii` only because of maskable
  spans. This is an engineering choice, not a privacy guarantee: a row has no per-field provenance (we
  cannot tell which alias or description came from which chunk, or whether it holds a masked value),
  and nothing re-checks a row's sources at read time, so there is no safe way to show part of one.
  It does not keep PII-derived text out of visible rows. An entity's rank is the minimum over its
  supporting chunks, and its aliases come from every `MENTIONED_IN` surface (its description may draw on
  any of them too), so an entity mentioned in
  one `internal` chunk and one `pii` chunk is visible to `internal` with the aliases taken from the `pii`
  chunk (an email written as a person's alias, say). That mixed-source exposure is the real residual risk
  of the graph; it needs per-field provenance or masking at write time, and is the trigger to revisit.
  Graph tools drop chunk text, search text and embeddings from every row, and rank `:Chunk` maps by
  their `sensitivity`, so `nexo_graph_query` returns no chunk text in a map or by name; a computed key
  (`c[k]`) still can, and needs database roles. `nexo_search` returns a masked chunk only when the query
  matches its masked text, without words that masking removed; ranking among matching chunks still
  uses the original text and embedding, so their order can shift with the masked words
  ([read-time redaction](../components/sensitive-data-detection.md#read-time-redaction)).

# Consequences

- Scalar projections of sensitive entities (`RETURN e.name`) are only hidden on Neo4j Enterprise; on
  Community, rely on chunk withholding and review.
- Regex detection is a baseline; real PHI workloads need a dedicated detector (Comprehend Medical or
  equivalent) under an approved data flow.

[^tools]: Clearance enforcement in tools
[^aws-health]: AWS guidance, RAG for healthcare use cases
