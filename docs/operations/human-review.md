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
  - id: ui
    resource: ../../src/nexo/review_ui.py
    title: Demo review page
  - id: state
    resource: ../../src/nexo/orchestration/state.py
    title: ReviewDecision, ReviewPacket
  - id: compliance
    resource: ../../src/nexo/orchestration/compliance.py
    title: auto_approves
---

# Trigger

When the [review policy](#review-policy) holds an answer, `nexo ask` (or `Runtime.start`) returns
`status: awaiting_review` with a `pending_review` packet (a `ReviewPacket`). Show
it again any time with `nexo status <thread_id>`. In code, `Runtime.pending_reviews()` lists every paused
thread, newest first, and `Runtime.review_passages(thread_id, clearance)` reads the cited chunks, masked
below their clearance ([ADR-0019](../decisions/adr-0019-review-surfaces.md)).

# Review policy

`spec.review` in `nexo.yaml` (`REVIEW_POLICY`) decides which answers wait for a person. The default is
`on-findings`. `make check` prints the policy in force.[^compliance]

| Policy | Approved without a person | Waits for a person | Recorded reviewer | Choose it for |
|---|---|---|---|---|
| `on-findings` (default) | No findings, at least one citation, and every cited chunk was fetched | Any answer with a finding (`block` or `warn`) | `system:auto` when approved without a person, else the person | Everyday use |
| `always` | None | Every answer | The person | Regulated or high-stakes use, where a person signs off every answer |
| `never` | No findings, or only `warn` findings | Any answer with a finding other than `warn`, including every `block` | `system:auto` when approved without a person, else the person | Trusted corpora and demos |

No policy approves an answer with a `block` finding. Those answers always wait for a person, who must
acknowledge the findings to approve them. A failed or empty investigation is still auto-rejected as
`reviewer: system`.

## `on-findings` (default)

A clean answer goes out without a pause. An answer is clean when compliance raised no finding at all,
it cites at least one chunk, and the agent fetched every chunk it cites. An answer with any finding
waits for a person, even if the finding is only a warning such as `budget_exhausted`. That includes a
run whose model asked for more tool calls than the budget allowed: it wanted evidence it did not get,
so its answer pauses. Use it for everyday use: a person sees only the answers a check flagged.

## `always`

Every answer waits for a person, including clean ones. This is how Nexo behaved before the policy
existed. Use it for regulated or high-stakes use, or wherever a named person must approve every
released answer.

## `never`

Answers with no findings or only `warn` findings go out without a pause. Despite the name, review is
not off: a `block` finding, or any severity other than `warn`, still waits for a person. Use it for
trusted corpora and demos, where a warning such as a hit budget is not worth a person's time.

## The record

An approval without a person is recorded like any other. The thread's `review` has
`reviewer: system:auto` and a comment naming the policy and any warnings. The compliance span carries
`nexo.review.reviewer=system:auto`, and every compliance span carries `nexo.review.policy`.
`nexo.env` always carries the policy, the default included, and is read after `.env`, so a
`REVIEW_POLICY` left in `.env` cannot override `nexo.yaml`. Only `REVIEW_POLICY` in the process
environment overrides it, and `make check` says so when it does.

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

5. **Confirm** the result shows `status: completed` and the expected `approval_status`. `completed`
   only means the run finished; `outcome` says how, e.g. `rejected by you@corp` or, when
   nothing reached review, `rejected: no releasable output`
   ([Request lifecycle](../architecture/request-lifecycle.md#result)).

# Demo review page

`make review-ui` (or `nexo review-ui`) serves a local page that lists paused questions and takes the
decision with Approve and Reject buttons.[^ui] It is for demos: it has no authentication, so anyone who
can reach the port can decide, and it is not part of `make up`. It binds `127.0.0.1` only and refuses any
other host. The reviewer is `NEXO_USER`, read-only; passages are read at the default clearance and
withheld ones say so. Use review over MCP for real work ([ADR-0019](../decisions/adr-0019-review-surfaces.md)).

# Rules

- `--acknowledge-findings` is required to approve when any finding is `block`. Without it the run stays
  paused and the packet gains an `error`.
- An invalid decision (an empty `--reviewer`, a comment over 2000 characters) is refused before the
  thread is touched: `nexo review` exits with 2 and names the field.
- The reviewer's id, comment and acknowledgement are stored in the thread's `review` state as the
  audit record.[^state]
- A rejected answer is never released; start a new thread with a refined question instead.
- Reviews can happen hours later and from another process: the pause is checkpointed (SQLite or
  Postgres).[^cli]

[^cli]: CLI
[^compliance]: auto_approves
[^state]: ReviewDecision, ReviewPacket
[^ui]: Demo review page
