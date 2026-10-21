---
type: Architecture Decision
title: "ADR-0013: Read-only MCP tools plus a phased Claude skill"
description: Expose retrieval, not the full workflow, over MCP; keep one clearance per server; and give Claude a checked, phased skill instead of relying on the model to use the tools well.
tags: [adr, mcp, skills, security]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: server
    resource: ../components/mcp-server.md
    title: MCP server
  - id: playbook
    resource: ../operations/claude-code.md
    title: Use from Claude Code
---

# Context

Clients other than Nexo's own LangGraph workflow (Claude Code, other agents) should be able to query the
knowledge graph. Left alone, a model often answers from what it already knows or reads only part of the
evidence; and MCP over stdio carries no user identity.

# Decision

- Expose **read-only retrieval tools** over MCP: index, search, chunks, entities, neighbors, read-only
  Cypher.[^server] The investigation workflow with human review stays in Nexo's CLI and `Runtime`.
- **One clearance per server** (`MCP_CLEARANCE`); run a server per clearance level.
- Ship a **skill** with fixed phases and checks (orient, locate, trace, read, answer) and an explicit
  citation rule, so tool use does not depend on the model choosing well.[^playbook]
- Default to **stdio**; HTTP binds localhost and needs an authenticating proxy to go further.

# Options considered

| Option | Against |
|---|---|
| Expose `nexo ask` / review over MCP | Long-running, human-gated workflow; poor fit for a tool call. `nexo ask` stays out; review tools came later ([ADR-0019](adr-0019-review-surfaces.md)). |
| Only `nexo_graph_query` | Forces every client to write Cypher; entity and neighbor tools are safer and parameterized. |
| Per-user clearance over MCP | Needs authentication the stdio transport does not have. |
| No skill, instructions only | The server's `instructions` help, but a skill makes the order and checks explicit. |

# Consequences

Tests keep the skill's tool list and `.mcp.json` in sync with the server. Any write access, or an
investigation tool, would need a new decision.

# Amendment: user-scoped Claude Code installation

The shipped `.mcp.json` and project skill remain unchanged. `nexo mcp install` now offers
project or user scope (also selected with `--scope`); `uninstall` reverses the chosen scope.
Registration belongs beside the MCP server command rather than stack setup because it configures
a client and does not provision services. User scope pins an absolute checkout/runtime and
settings directory, one explicit clearance, and `MCP_REVIEW_TOOLS=false`. It therefore exposes
only this decision's read-only retrieval set, even when the settings directory enables the
review extension from ADR-0019. The optional user skill is an unchanged copy with the same
pre-approved tool list. Scope changes availability across projects, not authorization.

Claude's CLI owns user configuration writes. Existing entries/skills require an explicit
update choice, and removal is scoped. See the [installation playbook](../operations/claude-code.md#use-nexo-in-every-project).

[^server]: MCP server
[^playbook]: Use from Claude Code
