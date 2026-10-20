---
type: Component
title: MCP server
description: "`nexo mcp`: the knowledge graph and chunk store as six read-only Model Context Protocol tools, plus tools to review paused investigations, with the investigator's safeguards and one clearance per server."
resource: ../../src/nexo/mcp_server.py
tags: [mcp, tools, retrieval, security]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: review
    resource: ../../src/nexo/mcp_review.py
    title: Review tools
  - id: server
    resource: ../../src/nexo/mcp_server.py
    title: Nexo MCP server
  - id: tests
    resource: ../../tests/test_mcp_server.py
    title: MCP server tests
  - id: mcp
    resource: ../references/model-context-protocol.md
    title: Model Context Protocol
---

# Tools

The six retrieval tools are annotated read-only, idempotent and closed-world.[^server]

| Tool | Returns | Use |
|---|---|---|
| `nexo_index` | Documents (within clearance), relation patterns with counts, retrieval mode, clearance, usage guidance | First call in a conversation |
| `nexo_search(query, k, doc_types)` | Snippets with chunk ids, ranked by hybrid BM25 + vector search, masked like `nexo_get_chunks` | Topics, codes, phrases |
| `nexo_get_chunks(chunk_ids)` | Exact chunk text (detected PII masked above clearance); `masked`, `withheld`, `not_found`, `invalid_or_over_limit` | The evidence to cite |
| `nexo_find_entities(name, entity_type, limit)` | Entities matching a name or alias, with keys and chunk ids | Starting points in the graph |
| `nexo_neighbors(entity_key, limit)` | Edges in both directions with certainty, date and chunk ids; `withheld` count | Direct relationships |
| `nexo_graph_query(cypher)` | Rows, `truncated`, `chunk_ids_found` | Multi-hop questions |

# Review tools

On by default (`MCP_REVIEW_TOOLS=true`); `false` leaves only the six tools above.[^review]

| Tool | Returns | Annotations |
|---|---|---|
| `nexo_pending_reviews(limit)` | Paused investigations, newest first: thread id, question, blocking, finding count | read-only |
| `nexo_get_review(thread_id)` | Question, draft answer, compliance findings, cited passages (masked or withheld above the server's clearance) | read-only |
| `nexo_submit_review(thread_id, decision, comment, acknowledge_findings)` | `submitted`, `approval_status`, `reviewer` | not read-only, not idempotent |

`nexo_submit_review` only proposes. It uses MCP elicitation (SDK `mcp` 2.1.1, `Resolve` and
`Elicit`, which work on both the 2026-07-28 and the older protocol): the client shows the person the
question, draft, findings and passages in a form with a required decision, and nothing is recorded
unless they accept it. Their choice wins over the proposal. A client that cannot show a form gets an
error with the `nexo review` command to run. Approving a draft with blocking findings needs the
person to tick `acknowledge_findings`. The reviewer is `NEXO_USER` (or the login name) unless the
person types another name on the form; the tool has no reviewer argument. A client that answers
forms automatically, without a person, defeats the check: use a client that asks.

# Safeguards

- **Read-only.** `nexo_graph_query` goes through the [Cypher guard](graph-store.md#read-only-enforcement)
  and a READ transaction; the lookup tools use parameterized Cypher, so user text is never spliced in.
- **Clearance.** One per server: `MCP_CLEARANCE`, defaulting to `DEFAULT_CLEARANCE`. Chunks above it go
  through the agent's `ChunkRedactor` ([read-time redaction](sensitive-data-detection.md#read-time-redaction),
  with the `SENSITIVE_DATA_PROVIDER` detector): a chunk above it only because of detected PII is shown with
  those spans masked (`[EMAIL]`, `[PHONE]`) and listed in `masked`; anything else above it, PHI included, is
  withheld whole. `nexo_search` masks the query before ranking, so it cannot probe for a detected value.
  Entities and edges above it are redacted or withheld
  ([sensitivity model](sensitive-data-detection.md#sensitivity-levels)), even when their only sources are
  maskable chunks ([ADR-0006](../decisions/adr-0006-sensitivity-and-caller-clearance.md)); the fact stays
  reachable through `nexo_search`. MCP over stdio has no per-user identity, so run one server per clearance.
- **Errors.** Anticipated failures (unsafe Cypher, unknown entity type, graph unavailable) come back as
  tool errors with a message; unexpected exceptions are masked and logged on the server.
- **Transport.** `stdio` by default. `--transport streamable-http` binds `127.0.0.1:8765` and has no
  authentication; put it behind something that authenticates before exposing it.
- **Clean stdout.** Over stdio, stdout carries the protocol, so the command logs to stderr; a test
  checks every stdout line is JSON-RPC.[^tests]

Telemetry: each tool runs inside an `execute_tool <name>` span. See the
[MCP specification](../references/model-context-protocol.md) and how Claude uses these tools:
[Use from Claude Code](../operations/claude-code.md).[^mcp]

[^server]: Nexo MCP server
[^tests]: MCP server tests
[^review]: Review tools
[^mcp]: Model Context Protocol
