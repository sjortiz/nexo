---
type: Component
title: Entity resolution
description: Maps extracted mentions onto canonical graph entities - exact keys, aliases, then a conservative model arbiter - with every merge recorded and reversible.
resource: ../../src/nexo/storage/resolution.py
tags: [module-a, entity-resolution, knowledge-graph]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: resolution
    resource: ../../src/nexo/storage/resolution.py
    title: EntityResolver
  - id: canonical
    resource: ../../src/nexo/storage/canonical_names.py
    title: Canonical names
  - id: merge
    resource: ../../src/nexo/storage/entity_merge.py
    title: Entity merge
  - id: cookbook
    resource: ../references/anthropic-claude-cookbook.md
    title: Claude Cookbook, knowledge graph guide
  - id: tests
    resource: ../../tests/test_resolution.py
    title: Resolution tests
---

# Why

Multi-hop questions only work when "Acme Holdings Inc.", "Acme" and "AHI" from different documents are
one node. Every knowledge-graph source Nexo follows treats this as essential; exact string keys alone
are not enough.[^cookbook]

# Steps (per entity type, cheapest first)

1. **Normalize the key.** `entity_key` = type + normalized name: case and punctuation folded, a leading
   "the" dropped, legal suffixes stripped for organizations (Inc, Corp, LLC, S.A., ...), honorifics for
   people (Dr, Mr, Sra, ...). Medication and condition names keep every token, including doses.
2. **Merge within the document.** A mention whose name or stated `aliases` overlap a longer mention's
   merges into the most complete name (`ALIAS`).
3. **Match existing entities.** Candidates come from `GraphStore.find_candidates`: same type, name or
   alias containing a token of the mention (text index). An identical key is `EXACT`; a name or alias
   matching exactly one candidate is `ALIAS`; an alias shared by several candidates is ambiguous and
   goes to step 4.
4. **Model arbitration** (`ENTITY_RESOLUTION=llm`). Unresolved mentions and their blocked candidates
   (names, aliases, descriptions) go to the model in batches of `RESOLUTION_BATCH_SIZE`. It may only
   answer a key it was shown, or null. The prompt says unsure means null: a wrong merge corrupts the
   graph, a missed merge only loses a link.[^resolution]

# Canonical names and merging

Resolution never merges across types, so a name the extractor types inconsistently (a team as
Organization in one chunk and Concept in another) becomes several entities. Two tools fix that:

- `CANONICAL_NAMES_PATH` (YAML/JSON, example `deploy/canonical-names.example.yaml`; keep a deployment's
  own in git-ignored `local/`): each entry has a name, a type and aliases. An extracted mention matching
  any of them, whatever its type, is rewritten to the canonical name and type before resolution; its
  wording is kept as an alias. Counted as `converted.canonical_name` in the ingest report.[^canonical]
- `nexo entities duplicates` lists names stored under more than one type, with mentions, relations
  and documents per entity. `nexo entities merge --into <key> <key>...` moves every mention and relation
  onto the kept entity, deletes the others, recomputes aliases and ranks, and records the merge in
  `CANONICAL_NAMES_PATH` so a re-ingest does not split it again.[^merge]

# Guarantees

- Never merges across entity types, even if the catalog returns other types.
- A model failure keeps mentions separate; ingestion never fails over resolution.
- Every mention writes `MENTIONED_IN {surface, resolution}` (`exact`, `alias`, `llm`, `new`,
  `stated_alias`), so a merge can be audited and found. `Entity.aliases` is derived from those edges,
  so withdrawing a document withdraws the aliases and merges it contributed.
- Triples whose head and tail merge into one entity are dropped (`self_loop_after_resolution`).

These behaviors, including the cases that must not merge, are covered by tests.[^tests]

# Modes

| `ENTITY_RESOLUTION` | Behavior |
|---|---|
| `off` | Normalized keys only. |
| `deterministic` | + in-document and graph alias matches; no model calls. |
| `llm` (default) | + model arbitration for the rest. |

[^resolution]: EntityResolver
[^canonical]: Canonical names
[^merge]: Entity merge
[^cookbook]: Claude Cookbook, knowledge graph guide
[^tests]: Resolution tests
