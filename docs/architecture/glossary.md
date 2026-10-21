---
type: Glossary
title: Glossary
description: One line per Nexo term - the name to use, the other names code, prompts and docs give it, and where it lives.
resource: ../../src/nexo/storage/schemas.py
tags: [architecture, glossary, naming]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T18:00:00Z }
sources:
  - id: schemas
    resource: ../../src/nexo/storage/schemas.py
    title: Pydantic schemas
  - id: chunk-store
    resource: ../../src/nexo/storage/vector_store.py
    title: ChunkStore contract and ChunkFilter
  - id: graph-store
    resource: ../../src/nexo/storage/graph_store.py
    title: Graph store (Cypher)
  - id: investigator
    resource: ../../src/nexo/agents/investigator.py
    title: Investigator agent (seeds)
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Investigator tools
  - id: redaction
    resource: ../../src/nexo/agents/redaction.py
    title: ChunkRedactor
  - id: state
    resource: ../../src/nexo/orchestration/state.py
    title: Graph state
  - id: service
    resource: ../../src/nexo/service.py
    title: Runtime (start, review, review_passages)
  - id: setup-schema
    resource: ../../src/nexo/setup/schema.py
    title: Setup file schema (nexo.yaml)
  - id: lock
    resource: ../../src/nexo/setup/lock.py
    title: Setup lock (nexo.lock)
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Hardware profiles, tiers and role sets
---

# Terms

Each line: **term**, what it is, *aliases* (other names for the same thing), where it lives.

* **chunk**: a stored span of one document's text, id `chk_` + 16 hex. *Aliases:* passage (prompts,
  `<passage>` tags, `search_chunks`, review "passages"), record (`ChunkRecord`). `storage/schemas.py`.[^schemas]
* **chunk store**: where chunks, their embeddings and search live; Neo4j `(:Chunk)` nodes by default
  ([ADR-0018](../decisions/adr-0018-chunks-in-neo4j.md)). *Aliases:* vector store (`vector_store.py`,
  `fetch_vector_chunks`, "vector store unavailable"). Contract `ChunkStore` in `storage/vector_store.py`.[^chunk-store]
* **seed**: chunk-store search hits handed to the agent before its first turn (`SEED_CHUNKS`, default 5;
  none for the `graph` strategy). *Aliases:* seed passages, `seed_chunk_ids`. `agents/investigator.py`.[^investigator]
* **entity**: an `(:Entity:<Type>)` node keyed `<Type>:<normalized name>` (`entity_key`). *Aliases:*
  node, `EntityRef` (as extracted), `ResolvedEntity` (after resolution). `storage/schemas.py`.
* **mention**: one surface form of an entity in one chunk, the `MENTIONED_IN {surface, resolution}` edge;
  surfaces become the entity's `aliases`. `storage/graph_store.py`.[^graph-store]
* **relation**: the fact's verb in UPPER_SNAKE_CASE (`Triple.relation`); with an ontology, a canonical
  relation or `RELATED_TO`. *Aliases:* predicate (the extracted wording, kept in `extracted_as`), relationship type,
  edge, hop (one entity-to-entity edge in `find_paths`). `storage/schemas.py`, `storage/graph_store.py`.
* **sensitivity**: the one ordered enum `public < internal < pii < phi` (`Sensitivity`). *Aliases:*
  level (prose), classification (`review_passages` key, `Sensitivity` docstring), rank (`.rank`, 0-3).
  On data: `sensitivity` (documents, chunks), `declared_sensitivity` (before detection raised it),
  `sensitivity_rank` (entities, edges: least sensitive supporting chunk).
* **clearance**: the highest sensitivity a caller may read, set by the service, never an LLM.
  *Aliases:* `caller_clearance` (state), `max_sensitivity` (`ChunkFilter`), `clearance_rank` (tools),
  `DEFAULT_CLEARANCE`, `MCP_CLEARANCE`. `storage/vector_store.py`, `orchestration/state.py`.[^state]
* **masked**: a chunk above clearance only because of detected PII, shown with spans replaced (`[EMAIL]`).
  *Aliases:* redacted (`nexo.chunks.redacted`, `ChunkRedactor`), MCP `masked` list. `agents/redaction.py`.[^redaction]
* **withheld**: a chunk or graph row not shown at all (PHI, declared level, outside scope). *Aliases:*
  graph maps replaced by `{"redacted": "above caller clearance"}`, `withheld` lists and counts in tool
  and MCP results, review passage `status: withheld`. `agents/tools.py`.[^tools]
* **thread_id**: one question's run, from start to review. *Aliases:* `request_id` (graph state and
  review packet, equal to `thread_id` from `Runtime.start`), `gen_ai.conversation.id` (traces),
  `configurable.thread_id` (LangGraph). `service.py`.[^service]
* **review packet**: what the compliance node hands a reviewer: request id, question, draft answer,
  citations, compliance report, `resume_with`. *Aliases:* `pending_review` (`RunResult`), interrupt value
  (LangGraph), HumanInterrupt (Agent Inbox, `to_human_interrupt`). `orchestration/graph.py`.
* **reviewer**: who decided, `ReviewDecision.reviewer`; `system:auto` is the
  [review policy](../operations/human-review.md#review-policy), `system` with `source: system` the
  auto-reject. `orchestration/compliance.py`, `orchestration/state.py`.
* **source**: one ingested document, `source_id` (stable id; re-ingesting replaces it). *Aliases:*
  document, `(:Document)`; `source_uri` is where it came from; `source` in tool and review results is a
  display label (title > section path); `source_ids` on entities lists supporting documents.
  `storage/schemas.py`.
* **casting**: one whole `nexo.yaml`, the setup file (`Casting`, `load_casting`). *Aliases:* setup file.
  `setup/schema.py`.[^setup-schema]
* **lock**: `nexo.lock`, the setup file resolved to components, images and each role's model; never
  secrets. `setup/lock.py`.[^lock]
* **stack**: one deployment, named by `metadata.name` (`NEXO_STACK`); it names the compose project,
  data directory and secrets section. `setup/schema.py`.
* **preset**: a starter `nexo.yaml` in `examples/setups` (`nexo setup init --preset mac-single`).
* **profile**: a machine's hardware name (`m1-max-64`, `gpu-24`) at `models.local.profile` /
  `models.remote.profile`. `local_profiles.py`.[^profiles]
* **tier**: the catalog entry for the largest memory size a profile fits (`TIERS`, `tier_gb`).
* **role set**: which roles a machine serves: `single` (all), `heavy` (agent, extraction), `light`
  (router, judge, embedding) (`ROLE_SETS`).
* **role**: one model job: `agent`, `extraction`, `router`, `judge`, `embedding` (`ROLES`, `models.roles`).

[^schemas]: Pydantic schemas
[^chunk-store]: ChunkStore contract and ChunkFilter
[^graph-store]: Graph store (Cypher)
[^investigator]: Investigator agent (seeds)
[^tools]: Investigator tools
[^redaction]: ChunkRedactor
[^state]: Graph state
[^service]: Runtime (start, review, review_passages)
[^setup-schema]: Setup file schema (nexo.yaml)
[^lock]: Setup lock (nexo.lock)
[^profiles]: Hardware profiles, tiers and role sets
