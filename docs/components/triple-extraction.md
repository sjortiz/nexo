---
type: Component
title: Triple extraction
description: Guided-JSON LLM extraction of entity-relation-entity triples, validated by Pydantic and grounded in the chunk text.
resource: ../../src/nexo/storage/schemas.py
tags: [module-a, extraction, pydantic, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: types
    resource: ../../src/nexo/storage/entity_types.py
    title: Entity types
  - id: schemas
    resource: ../../src/nexo/storage/schemas.py
    title: Extraction schemas
  - id: llm
    resource: ../../src/nexo/llm.py
    title: invoke_structured
  - id: cookbook
    resource: ../references/anthropic-claude-cookbook.md
    title: Anthropic Claude Cookbook, knowledge graph guidance
---

# Schema

```json
{"triples": [{
  "head": {"name": "Acme Holdings Inc.", "type": "Organization", "description": "", "aliases": ["AHI"],
           "proposed_codes": {}},
  "relation": "ACQUIRED",
  "tail": {"name": "Beta Labs", "type": "Organization"},
  "evidence": "Acme Holdings Inc. acquired Beta Labs",
  "certainty": "asserted",
  "valid_at": "2024-03-01"
}]}
```

| Field | Rule |
|---|---|
| `head` / `tail` | Most complete name (including names from the preceding text) with pronouns resolved; `type` from the closed entity set. |
| `aliases` | Other names the passage uses for the same entity; feeds [entity resolution](entity-resolution.md). |
| `relation` | Normalized to UPPER_SNAKE_CASE (`works for`, `worksFor` → `WORKS_FOR`); rejected if it cannot be. Mapped onto the [relation ontology](relation-ontology.md) when one is configured, whose relations the prompt lists. |
| `evidence` | Verbatim quote (≤ 400 chars) from the passage. |
| `certainty` | `asserted`, `possible` (hedged) or `negated` (ruled out). |
| `valid_at` | Only when the passage states the date. Dates and amounts never become entities: quantities go in `attributes` (see [amounts](ingest-exclusions.md#amounts-as-entities)). |
| `proposed_codes` | Only codes printed in the passage; unverified until checked by a terminology service. |

All models use `extra="forbid"`, so invented keys are validation errors.[^schemas]

# Entity types

The built-in types are Person, Organization, Location, Product, Event, Concept, Condition, Medication
and Procedure, each shown to the extractor with what it means (Person is a named individual, never a
group). `ENTITY_TYPES_PATH` picks which built-ins a deployment extracts and adds its own, each with a
description:[^types]

```yaml
builtin: [Person, Organization, Location, Product, Event, Concept]
custom:
  - name: Contract
    description: a signed agreement between parties, with its terms
```

The extraction schema only accepts the configured names, so guided decoding keeps the model inside
them; a mention typed outside them is dropped as `dropped.entity_type_not_enabled`. Type names become
Neo4j labels, so they must be plain identifiers (`^[A-Z][A-Za-z0-9]{0,39}$`). Entities of different
types never merge, so changing types means re-ingesting.

# How the call works

`invoke_structured` binds `response_format = {type: json_schema, schema: <Pydantic JSON schema>}`, which
vLLM enforces with guided decoding. Output is then validated by Pydantic; on failure the model sees its
own output plus the validation errors and retries, up to `EXTRACTION_ATTEMPTS` (3).[^llm]

# Grounding

Following the cookbook guidance (closed types, specific directional predicates, facts stated in the
text only, a quote per fact),[^cookbook] a triple is kept only if its `evidence` appears in the chunk
after case and whitespace folding (`REQUIRE_GROUNDED_EVIDENCE=true`). This removes hallucinated facts
at the cost of dropping paraphrased quotes; the count shows up in the ingestion report as
`dropped.evidence_not_in_chunk`. The preceding-chunk context never counts as evidence: a quote that
only appears there is dropped.

[^types]: Entity types
[^schemas]: Extraction schemas
[^llm]: invoke_structured
[^cookbook]: Anthropic Claude Cookbook, knowledge graph guidance
