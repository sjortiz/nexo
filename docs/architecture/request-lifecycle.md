---
type: Sequence
title: Request lifecycle
description: One question from intake to an approved answer, step by step.
resource: ../../src/nexo/service.py
tags: [architecture, workflow, human-in-the-loop]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: service
    resource: ../../src/nexo/service.py
    title: Runtime (start, review, status)
  - id: graph
    resource: ../../src/nexo/orchestration/graph.py
    title: Orchestration graph
---

# Steps

1. **Start.** `Runtime.start(query, thread_id, clearance)` opens a `nexo.request` span and streams the
   graph with `{user_query, request_id, caller_clearance}`. `caller_clearance` comes from the
   authenticated caller, never from an LLM.[^service]
2. **Router.** Rejects empty or over-long queries without calling a model, then asks the router LLM for
   a `RouteDecision` (target, strategy, optional doc-type/date filters). Invalid or unavailable router
   output fails closed to `rejected`. Rejected requests end with `approval_status: not_required`.
3. **Investigator.** Runs the Strands agent inside a `@task`, scoped by the router's strategy, filters
   and the caller's clearance. The agent's output is appended to `agent_outputs`. See
   [the handoff](langgraph-strands-handoff.md).
4. **Compliance.** Runs [deterministic checks](../components/compliance-checks.md). A failed or empty
   investigation is auto-rejected with no human step. Otherwise the node calls `interrupt()` with a
   review packet; the run pauses and its state is checkpointed.[^graph]
5. **Review.** `Runtime.review(thread_id, decision)` resumes with `Command(resume=decision)`. Invalid
   input, or an approval of blocking findings without `acknowledged_findings: true`, pauses again with
   an `error` in the packet.
6. **Release.** On approval, `final_response` is set to the draft answer; on rejection it stays `null`.

# Result

`Runtime.start`, `review` and `status` return a `RunResult`. Its `status` is the lifecycle state only:
`awaiting_review` or `completed`, and a completed thread may have been approved or rejected. Read
`approval_status` for the decision and `outcome` for a one-line account of it:[^service]

| `outcome` | When |
|---|---|
| `awaiting review` | Paused at the compliance interrupt. |
| `approved by <reviewer>` | A reviewer or the review policy approved the draft. |
| `rejected by <reviewer>` | A reviewer or the review policy rejected the draft. |
| `rejected: no releasable output` | The compliance node rejected it (`review.source: system`): the agent failed or returned no answer. |
| `rejected: router refused` | The router refused the question (`approval_status: not_required`). |

`nexo ask`, `review` and `status` log it to stderr after a completed status (the JSON result goes to stdout), e.g.
`thread 3f2a…: completed (rejected: no releasable output)`. Logs are exported, so `outcome` carries
no free text: the reviewer's comment is in `review.comment` and the router's reason in
`routing_reason`, which stay in the result and are exported only with content capture
([ADR-0009](../decisions/adr-0009-opentelemetry-genai-conventions.md)). A reviewer may be named
`system`; only `review.source: system` marks the compliance node's own rejection.

# State

| Key | Written by | Meaning |
|---|---|---|
| `user_query` | caller | The question. |
| `request_id` | router | Correlation id; equals the thread id when started through `Runtime`. |
| `caller_clearance` | caller | Highest [sensitivity](../components/sensitive-data-detection.md) the caller may see. |
| `routing_target` | router | `investigator` or `rejected`. |
| `routing_reason` | router | One-sentence reason. |
| `investigation_scope` | router | Strategy and chunk filter handed to the agent. |
| `agent_outputs` | investigator | Append-only list of agent outputs (audit trail). |
| `compliance_report` | compliance | Findings and the `blocking` flag. |
| `approval_status` | router, compliance | `pending`, `approved`, `rejected` or `not_required`. |
| `review` | compliance | The reviewer's decision, or `reviewer: system` and `source: system` on auto-reject. |
| `final_response` | router, compliance | Released answer, only after approval. |

# Examples

```bash
nexo ask "How is Acme Holdings connected to Gamma Clinics?" --clearance internal
# -> {"thread_id": "...", "status": "awaiting_review", "pending_review": {...}}
nexo review <thread_id> --approve --reviewer analyst@corp
```

[^service]: Runtime (start, review, status)
[^graph]: Orchestration graph
