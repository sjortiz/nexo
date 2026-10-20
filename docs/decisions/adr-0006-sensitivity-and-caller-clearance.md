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
  spans. A row has no span-level provenance: names, aliases, descriptions and edge evidence were written
  by the extraction model from the unmasked text and can hold the detected value verbatim or reformatted
  (a phone number spelled differently, an email as a person's alias), which the detector may not catch on
  a short field. Showing the row would also need every supporting chunk re-checked at read time, and an
  entity's existence or an edge is itself a fact about the person. The fact stays reachable as text:
  `nexo_search` returns the masked chunk. Revisit if rows gain per-field provenance or masking at write
  time.

# Consequences

- Scalar projections of sensitive entities (`RETURN e.name`) are only hidden on Neo4j Enterprise; on
  Community, rely on chunk withholding and review.
- Regex detection is a baseline; real PHI workloads need a dedicated detector (Comprehend Medical or
  equivalent) under an approved data flow.

[^tools]: Clearance enforcement in tools
[^aws-health]: AWS guidance, RAG for healthcare use cases
