"""The Claude Code skill and .mcp.json stay in sync with the MCP server."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import yaml
from mcp.client import Client

from nexo.mcp_server import build_mcp_server
from tests.test_mcp_server import RoutedGraph

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / ".claude" / "skills" / "nexo-knowledge" / "SKILL.md"


def _skill() -> tuple[dict, str]:
    """Return the skill's parsed frontmatter and markdown body."""
    _, front, body = SKILL.read_text().split("---\n", 2)
    return yaml.safe_load(front), body


def _server_tools(settings, chunk_store) -> set[str]:
    """Return the tool names the MCP server actually registers."""
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings
    )

    async def run():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    return asyncio.run(run())


def test_skill_frontmatter_is_valid():
    """The name matches its directory and the description fits Claude Code's limits."""
    front, body = _skill()
    assert (
        re.fullmatch(r"[a-z0-9-]{1,64}", front["name"])
        and front["name"] == SKILL.parent.name
    )
    assert 0 < len(front["description"]) <= 1024 and "Use when" in front["description"]
    assert body.strip()


def test_skill_allows_exactly_the_servers_tools(settings, chunk_store):
    """allowed-tools lists every server tool and nothing else."""
    front, _ = _skill()
    allowed = {t.strip() for t in front["allowed-tools"].split(",")}
    assert allowed == {
        f"mcp__nexo__{name}" for name in _server_tools(settings, chunk_store)
    }


def test_skill_body_only_mentions_real_tools_and_starts_with_the_index(
    settings, chunk_store
):
    """The body explains every real tool, no invented ones, and introduces nexo_index first."""
    _, body = _skill()
    mentioned = set(re.findall(r"`(nexo_[a-z_]+)", body))
    tools = _server_tools(settings, chunk_store)
    assert mentioned <= tools, f"skill mentions unknown tools: {mentioned - tools}"
    assert mentioned == tools, f"skill never explains: {tools - mentioned}"
    assert body.index("nexo_index") < min(
        body.index(name) for name in tools - {"nexo_index"}
    )


def test_project_mcp_config_launches_nexo_mcp():
    """.mcp.json registers the server as `nexo` running `nexo mcp` through uv."""
    config = json.loads((ROOT / ".mcp.json").read_text())
    # The skill's mcp__nexo__ tool prefix depends on this name.
    server = config["mcpServers"]["nexo"]
    # --quiet keeps uv's own messages off stdout, which carries the MCP protocol.
    assert server["command"] == "uv"
    assert server["args"] == ["run", "--quiet", "nexo", "mcp"]
