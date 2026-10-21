---
type: Playbook
title: Deleting a source
description: How an operator removes a document and everything only it supported from the graph and the chunk store, and what the audit record says.
resource: ../../src/nexo/service.py
tags: [operations, administration, audit]
status: draft
generated: { by: claude-code/claude-fable-5-1, at: 2026-10-21T15:00:00Z }
sources:
  - id: service
    resource: ../../src/nexo/service.py
    title: remove_source
  - id: graph
    resource: ../../src/nexo/storage/graph_store.py
    title: GraphStore.remove_source
  - id: schema
    resource: ../../src/nexo/setup/schema.py
    title: spec.admin
---

# When

A document was ingested by mistake, has been withdrawn, or must leave the knowledge base. Ingesting
it again replaces it in place; `nexo delete` is for taking it out.

# Who

Only an operator on the allowlist. `nexo.yaml` names them and `nexo setup` renders the list as
`DELETE_OPERATORS` (always, so a stale value in `.env` cannot widen it):[^schema]

```yaml
spec:
  admin:
    deleteOperators: [ana, bob]
```

The caller is the same `user.id` traces carry: `NEXO_USER`, else the login name. Anyone else gets
`<user> is not a delete operator (DELETE_OPERATORS)`, exit code 2, and nothing changes. An empty list
is the default: nobody can delete until someone is named.

# What it does

```
nexo delete acme-portfolio-note
```

runs the detach step of a re-ingestion with nothing written after it:[^graph] the document node, its
chunks (graph and chunk store) and its mentions go; entities and edges that other documents still
support keep them, with the removed chunks taken out of their provenance; entities and edges nothing
supports any more are deleted; aliases, sensitivity ranks and the `:PII`/`:PHI` labels of what remains
are recomputed. The command prints the record as JSON:

```json
{"source_id": "acme-portfolio-note", "operator": "ana", "chunks_removed": 3, "entities_touched": 4}
```

# The audit record

Every removal is one log record on the `nexo.audit` logger, at `INFO` whatever `LOG_LEVEL` says, so
with `OTEL_LOGS_EXPORTER=otlp` it always reaches the collector, and one `nexo.delete` event on the
`delete_source` span. Both carry the source id, the operator and the counts above; neither carries
text.[^service]

[^service]: remove_source
[^graph]: GraphStore.remove_source
[^schema]: spec.admin
