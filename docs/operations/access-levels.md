---
type: Playbook
title: Access levels
description: How a chunk gets its access level, why, and what a caller with each clearance sees; nexo access shows it per document.
resource: ../../src/nexo/access_report.py
tags: [operations, sensitivity, clearance, access-control]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-22T12:00:00Z }
sources:
  - id: report
    resource: ../../src/nexo/access_report.py
    title: access_report
  - id: redaction
    resource: ../../src/nexo/agents/redaction.py
    title: ChunkRedactor
  - id: adr
    resource: ../decisions/adr-0006-sensitivity-and-caller-clearance.md
    title: ADR-0006 sensitivity and caller clearance
---

# Levels

Four ordered levels: `public` < `internal` < `pii` < `phi`. Everything below is the same scale.

# How a chunk gets its level

| Step | Field on the chunk | Set by |
|---|---|---|
| Declared | `declared_sensitivity` | `nexo ingest --sensitivity` (default `internal`) for the whole document |
| Detected | `sensitivity_reasons` | The detector (`SENSITIVE_DATA_PROVIDER`): the categories it found above the declared level, e.g. `PHONE`, `EMAIL` |
| Effective | `sensitivity` | The higher of the two; this is the level everything else uses |

With `SENSITIVE_DATA_POLICY=redact`, detected values are replaced before storage, so the level stays
the declared one and `sensitivity_reasons` is empty.

Entities and relations take the level of their least sensitive supporting chunk (`sensitivity_rank`,
0 = public … 3 = phi), and entities carry `:PII` / `:PHI` labels; an edge's evidence quote keeps the
level of the one chunk it quotes.[^adr]

# Who sees what

A caller's clearance comes from the caller, never from a model: `DEFAULT_CLEARANCE` for `nexo ask`
unless `--clearance` is given, `MCP_CLEARANCE` for `nexo mcp`. Per chunk:[^redaction]

| Chunk level vs clearance | The caller sees |
|---|---|
| At or below clearance | The chunk as stored |
| Above clearance only because of detected PII spans | The chunk **masked** (`[PHONE]`, `[EMAIL]`, ...) |
| Above clearance for any other reason (declared level, PHI) | Nothing: **withheld** |

# Check a corpus

`nexo access` (or `nexo access --source <id>`) prints, per document, the declared level, how many
chunks are at each level, the detected reasons, and what each clearance would see:[^report]

```json
{"source_id": "memo-1", "declared": "internal",
 "chunks": {"internal": 13, "pii": 2}, "reasons": {"PHONE": 2},
 "sees": {"public": {"withheld": 15}, "internal": {"visible": 13, "masked": 2},
          "pii": {"visible": 15}, "phi": {"visible": 15}}}
```

Chunks ingested before `sensitivity_reasons` existed show no reasons until they are re-ingested.

[^report]: access_report
[^redaction]: ChunkRedactor
[^adr]: ADR-0006 sensitivity and caller clearance
