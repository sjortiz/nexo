---
type: Control Catalog
title: Compliance checks
description: Deterministic findings computed from the agent output and shown to the human reviewer before release.
resource: ../../src/nexo/orchestration/compliance.py
tags: [module-c, compliance, human-in-the-loop, grounding]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: compliance
    resource: ../../src/nexo/orchestration/compliance.py
    title: run_compliance_checks
---

# Findings

`block` findings require `acknowledged_findings: true` to approve. `warn` findings are informational.

| Code | Severity | Raised when |
|---|---|---|
| `investigation_failed` | block | The agent raised, timed out or produced no output. The run is auto-rejected with no human step. |
| `empty_answer` | block | The agent returned no text (auto-rejected). |
| `no_citations` | block | The answer cites no `[chunk:<id>]` (a bare hash counts only if it names exactly one chunk the agent read). |
| `ungrounded_citations` | block | A cited id was never returned to the agent; the citation is fabricated or guessed. |
| `phi_in_evidence` | block | A cited chunk is classified `phi`; confirm minimum-necessary disclosure. |
| `pii_in_evidence` | warn | A cited chunk is classified `pii` (and none is `phi`). |
| `sensitive_data_in_answer` | block | The configured detector finds identifiers in the answer text itself. |
| `budget_exhausted` | warn | `stop_reason` starts with `limit_`; the answer may be incomplete. |
| `tool_errors` | warn | Some tool calls failed. |
| `evidence_withheld` | warn | Chunks were withheld for clearance or scope. |

# Properties

- **Pure.** No I/O beyond the configured detector, so re-running on resume gives the same report.
- **Evidence-based.** Grounding compares citations with the tool-maintained ledger, not with the
  model's own claims.[^compliance]

[^compliance]: run_compliance_checks
