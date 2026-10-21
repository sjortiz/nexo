---
type: Component
title: Ingest exclusions and amount entities
description: Rules that keep chosen content out of document, search or extraction stages, and the AMOUNT_ENTITIES check that turns quantities named as entities into attributes.
resource: ../../src/nexo/storage/exclusions.py
tags: [module-a, ingestion, configuration, knowledge-graph]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T12:00:00Z }
sources:
  - id: exclusions
    resource: ../../src/nexo/storage/exclusions.py
    title: IngestExclusions
  - id: amounts
    resource: ../../src/nexo/storage/amounts.py
    title: is_amount
  - id: example
    resource: ../../deploy/ingest-exclusions.example.yaml
    title: Example exclusions
  - id: ingest
    resource: ../../src/nexo/storage/ingest.py
    title: KnowledgeGraphIngestor
---

# Why

Not every part of a document should feed every stage. Screenshot descriptions written by an upstream
pipeline are worth searching but turn product tiles, prices and ratings into graph facts; boilerplate
helps nothing. Exclusions say, per rule, which stages must not see which content, without editing
the source.[^exclusions]

# Stages

| Stage | What leaves | Still stored and citable |
|---|---|---|
| `document` | Removed before chunking. | No |
| `search` | Left out of the embedded and full-text indexed text. | Yes |
| `extraction` | Left out of the text sent to triple extraction, and of evidence grounding. | Yes |

# Rules

`INGEST_EXCLUSIONS_PATH` names a YAML or JSON file:[^example]

```yaml
exclude:
  - name: image-descriptions
    start: '^<!-- image:'
    while: '^>'
    stages: [extraction]
    doc_types: [web-export]
```

| Field | Meaning |
|---|---|
| `name` | Unique; reported in `excluded` and `dropped`. |
| `stages` | One or more of `document`, `search`, `extraction`. |
| `doc_types` | Optional: only documents ingested with these `--doc-type` values. |
| `lines` | Every line matching the regex. |
| `start` + `while` | From a line matching `start`, continuing while lines match `while`. |
| `start` + `end` | From a line matching `start` through the line matching `end` (or the end of the document). |
| `entity` | Extracted entities whose name matches; `stages` must be `[extraction]`. |

Each rule has exactly one selector. Regions are found once over the whole document and carried into
the chunks as marker lines, so a region split across chunks is excluded in each; a chunk whose
extraction text is empty costs no model call.[^ingest] Stored chunk text, and so the `chunk_id`, is the
chunk without its marker lines. Chunk sizes count the markers, so chunk boundaries can move slightly
when a rule matches.

# Amounts as entities

The extraction prompt says quantities are never entities and gives them a place: `attributes`
(`{entity, name, value, evidence}`) next to `triples`. A model may still name `4.8 stars` as a tail.
`AMOUNT_ENTITIES` decides what happens after validation:[^amounts]

| Value | Effect | Reported as |
|---|---|---|
| `convert` (default) | `Gamma Clinics HAS_RATING "4.8 stars"` becomes the attribute `rating = 4.8 stars` on Gamma Clinics. | `converted.amount_entity` |
| `drop` | The triple is discarded. | `dropped.amount_entity` |
| `keep` | No check. | - |

A name counts as an amount only when all of it is a quantity, price, percentage or date: `$35`,
`1.6k reviews`, `60 days`, `2024-03-01`; `$5 Lunch Special`, `3M` and `3 Musketeers` stay names. A triple
with amounts at both ends is dropped.

Attributes are stored on the entity's `MENTIONED_IN` edge for the chunk as `attributes_json`, so
re-ingesting or deleting the document withdraws them with the mention.

# Report

`nexo ingest` reports `excluded` (regions each rule matched), `dropped` (including
`excluded_entity:<rule>`, `amount_entity`, `attribute_evidence_not_in_chunk`), `converted` and
`attributes_written`.

[^exclusions]: IngestExclusions
[^amounts]: is_amount
[^example]: Example exclusions
[^ingest]: KnowledgeGraphIngestor
