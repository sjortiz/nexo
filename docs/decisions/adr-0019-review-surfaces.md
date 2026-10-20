---
type: Architecture Decision
title: "ADR-0019: Review surfaces"
description: One review core in Runtime, three ways to reach it; review over MCP is the supported default.
tags: [adr, human-review, mcp]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T22:00:00Z }
sources:
  - id: service
    resource: ../../src/nexo/service.py
    title: Runtime
  - id: review
    resource: ../operations/human-review.md
    title: Human review
---

# Context

A question stops at the compliance node until a person decides ([ADR-0003](adr-0003-durable-checkpoints-for-review.md)).
Today the only way to decide is `nexo review` on the command line, which needs the thread id and the
packet in hand. Reviewers want an inbox, and AI clients already talk to Nexo over MCP
([ADR-0013](adr-0013-read-only-mcp-and-skill.md)).

# Decision

Every surface calls the same `Runtime` methods:[^service] `pending_reviews` lists the paused threads,
`status` returns one packet, `review_passages` reads the cited chunks for a reviewer's clearance (masked
by the read-time redactor), and `review` records the decision. No surface owns review state.

| Surface | Status | Why |
|---|---|---|
| Review over MCP | Supported default | The reviewer is already in an MCP client; the server asks the human to confirm, so the model cannot approve alone. |
| LangChain Agent Inbox | Opt-in | An inbox UI for teams that already run one. |
| Local web page | Demo only | Shows the flow without an MCP client; no authentication, loopback only (`make review-ui`, [Human review](../operations/human-review.md#demo-review-page)). |
| `nexo review` | Always available | The fallback every other surface can print. |

# Consequences

A new surface needs no change to the graph or the checkpoints.[^review] Each surface must show the compliance
findings and require acknowledgement to approve a blocked draft; the graph enforces that either way.

[^service]: Runtime
[^review]: Human review
