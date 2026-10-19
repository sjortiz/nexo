---
type: Reference
title: Model Context Protocol
description: The open protocol Nexo's MCP server speaks, and the Python SDK features it uses.
resource: https://modelcontextprotocol.io/
tags: [reference, mcp, tools]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: spec
    resource: https://modelcontextprotocol.io/specification/latest
    title: MCP specification
  - id: sdk
    resource: https://github.com/modelcontextprotocol/python-sdk
    title: MCP Python SDK
  - id: sdk-v2
    resource: https://py.sdk.modelcontextprotocol.io/v2/
    title: MCP Python SDK v2 documentation
---

# Role in Nexo

MCP is an open protocol for exposing tools to model clients over JSON-RPC (stdio or HTTP).[^spec] Nexo's
[MCP server](../components/mcp-server.md) uses it so Claude Code and other clients can query the
knowledge graph without Nexo's own agent. Pinned as `mcp>=2.1,<3`.

# SDK features used (v2)

| Feature | Use |
|---|---|
| `MCPServer(name, title, instructions)` | Server identity and usage instructions sent at initialization.[^sdk-v2] |
| `@server.tool(annotations=ToolAnnotations(read_only_hint=True, ...), structured_output=True)` | Read-only tools with JSON results. |
| `ToolError` | Anticipated failures reach the model with a message; other exceptions are masked. |
| `run(transport="stdio" \| "streamable-http", host, port)` | `nexo mcp` transports. |
| `Client(server)` and `Client(StdioServerParameters(...))` | Tests over the real protocol, in-process and as a subprocess.[^sdk] |

v2 renamed `FastMCP` to `MCPServer`; code written for v1 does not run unchanged.

[^spec]: MCP specification
[^sdk]: MCP Python SDK
[^sdk-v2]: MCP Python SDK v2 documentation
