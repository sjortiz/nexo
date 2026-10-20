---
type: Component
title: MCP server
description: "`nexo mcp`: the knowledge graph and chunk store as six read-only Model Context Protocol tools, with the investigator's safeguards and one clearance per server."
resource: ../../src/nexo/mcp_server.py
tags: [mcp, tools, retrieval, security]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
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

All tools are annotated read-only, idempotent and closed-world.[^server]

| Tool | Returns | Use |
|---|---|---|
| `nexo_index` | Documents (within clearance), relation patterns with counts, retrieval mode, clearance, usage guidance | First call in a conversation |
| `nexo_search(query, k, doc_types)` | Snippets with chunk ids, ranked by hybrid BM25 + vector search | Topics, codes, phrases |
| `nexo_get_chunks(chunk_ids)` | Exact chunk text; `withheld`, `not_found`, `invalid_or_over_limit` | The evidence to cite |
| `nexo_find_entities(name, entity_type, limit)` | Entities matching a name or alias, with keys and chunk ids | Starting points in the graph |
| `nexo_neighbors(entity_key, limit)` | Edges in both directions with certainty, date and chunk ids; `withheld` count | Direct relationships |
| `nexo_graph_query(cypher)` | Rows, `truncated`, `chunk_ids_found` | Multi-hop questions |

# Safeguards

- **Read-only.** `nexo_graph_query` goes through the [Cypher guard](graph-store.md#read-only-enforcement)
  and a READ transaction; the lookup tools use parameterized Cypher, so user text is never spliced in.
- **Clearance.** One per server: `MCP_CLEARANCE`, defaulting to `DEFAULT_CLEARANCE`. Chunks above it are
  withheld whole (no read-time masking here); entities and edges above it are redacted or withheld
  ([sensitivity model](sensitive-data-detection.md#sensitivity-levels)). MCP over stdio has no per-user
  identity, so run one server per clearance.
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
[^mcp]: Model Context Protocol
