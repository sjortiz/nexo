---
type: Playbook
title: Human review
description: How a reviewer reads the review packet and approves or rejects an investigation.
resource: ../../src/nexo/cli.py
tags: [operations, human-in-the-loop, compliance]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: cli
    resource: ../../src/nexo/cli.py
    title: CLI
  - id: state
    resource: ../../src/nexo/orchestration/state.py
    title: ReviewDecision
---

# Trigger

`nexo ask` (or `Runtime.start`) returns `status: awaiting_review` with a `pending_review` packet. Show
it again any time with `nexo status <thread_id>`. In code, `Runtime.pending_reviews()` lists every paused
thread, newest first, and `Runtime.review_passages(thread_id, clearance)` reads the cited chunks, masked
below their clearance ([ADR-0019](../decisions/adr-0019-review-surfaces.md)).

# Steps

1. **Read the draft answer** and its `citations`.
2. **Open the cited chunks** and confirm each claim is stated there. Citations are only checked for
   having been fetched, not for supporting the sentence they follow.
3. **Read `compliance_report.findings`.** For each `block` finding, decide whether release is
   justified. See [Compliance checks](../components/compliance-checks.md).
4. **Decide:**

   ```bash
   nexo review <thread_id> --approve --reviewer you@corp --comment "verified against cited chunks"
   nexo review <thread_id> --approve --reviewer you@corp --acknowledge-findings --comment "PHI release approved: treatment team"
   nexo review <thread_id> --reject  --reviewer you@corp --comment "path via Beta Labs is unverified"
   ```

   From an MCP client, ask it to list the pending reviews and propose a decision: your client
   shows a form with the question, draft, findings and passages, and the decision is recorded only
   when you accept it ([MCP server](../components/mcp-server.md#review-tools)). A client without
   forms is refused and told the command above.

5. **Confirm** the result shows `status: completed` and the expected `approval_status`.

# Rules

- `--acknowledge-findings` is required to approve when any finding is `block`. Without it the run stays
  paused and the packet gains an `error`.
- The reviewer's id, comment and acknowledgement are stored in the thread's `review` state as the
  audit record.[^state]
- A rejected answer is never released; start a new thread with a refined question instead.
- Reviews can happen hours later and from another process: the pause is checkpointed (SQLite or
  Postgres).[^cli]

[^cli]: CLI
[^state]: ReviewDecision
