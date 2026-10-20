"""MCP server tools, exercised over the real protocol (in-process and stdio)."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types import LATEST_PROTOCOL_VERSION
from neo4j.exceptions import ServiceUnavailable

from nexo.mcp_server import build_mcp_server
from nexo.storage.graph_store import ReadResult
from nexo.storage.schemas import Sensitivity
from tests.conftest import make_chunk

TOOLS = {
    "nexo_index",
    "nexo_search",
    "nexo_get_chunks",
    "nexo_find_entities",
    "nexo_neighbors",
    "nexo_graph_query",
}


class RoutedGraph:
    """Returns canned rows for the first route whose needle appears in the Cypher."""

    def __init__(
        self,
        routes: dict[str, list[dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.routes = routes or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def run_read_query(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> ReadResult:
        """Record the query, then raise the configured error or return the routed rows."""
        self.calls.append((cypher, parameters))
        if self.error:
            raise self.error
        for needle, rows in self.routes.items():
            if needle in cypher:
                return ReadResult(rows=json.loads(json.dumps(rows)), truncated=False)
        return ReadResult(rows=[], truncated=False)

    def describe_schema(self) -> str:
        """Return a fixed one-pattern schema summary."""
        return "(:Organization)-[:ACQUIRED]->(:Organization)  x3"


def call(server, tool: str, arguments: dict[str, Any] | None = None):
    """Call one tool on the server through an in-process MCP client."""

    async def run():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments or {})

    return asyncio.run(run())


def data(result) -> Any:
    """Assert a tool call succeeded and return its structured result, unwrapped."""
    assert not result.is_error, result.content[0].text
    content = result.structured_content
    return (
        content["result"]
        if isinstance(content, dict) and set(content) == {"result"}
        else content
    )


@pytest.fixture(name="stored")
def fixture_stored(chunk_store):
    """Store one internal and one PHI chunk that share an order number."""
    public = make_chunk("deals", 0, "Acme Holdings acquired Beta Labs. Order XR-7731.")
    phi = make_chunk(
        "clinic",
        0,
        "Patient Jane Roe was treated by Beta Labs. Order XR-7731.",
        Sensitivity.PHI,
    )
    chunk_store.upsert([public, phi], [public.text, phi.text])
    return public, phi


def test_all_tools_are_listed_as_read_only(settings, chunk_store):
    """Exactly the expected tools are listed, each described and annotated read-only."""
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings
    )

    async def run():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert (
            tool.annotations.read_only_hint is True
            and tool.annotations.destructive_hint is False
        )
        assert tool.description


def test_index_lists_documents_within_clearance_and_the_schema(settings, chunk_store):
    """nexo_index hides documents above clearance, counts them, and includes schema and usage."""
    graph = RoutedGraph(
        {
            "MATCH (d:Document)": [
                {
                    "source_id": "deals",
                    "title": "Deals",
                    "doc_type": "memo",
                    "effective_date": None,
                    "sensitivity": "internal",
                    "chunks": 3,
                },
                {
                    "source_id": "clinic",
                    "title": "Clinic",
                    "doc_type": "note",
                    "effective_date": None,
                    "sensitivity": "phi",
                    "chunks": 2,
                },
            ]
        }
    )
    index = data(
        call(
            build_mcp_server(graph=graph, chunks=chunk_store, settings=settings),
            "nexo_index",
        )
    )
    assert [d["source_id"] for d in index["documents"]] == ["deals"]
    assert index["documents_withheld"] == 1 and index["clearance"] == "internal"
    assert "ACQUIRED" in index["schema"] and "nexo_get_chunks" in index["how_to"]


def test_search_is_hybrid_and_respects_clearance(settings, chunk_store, stored):
    """Keyword search finds exact codes and returns PHI chunks only with PHI clearance."""
    public, phi = stored
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings
    )
    hits = data(call(server, "nexo_search", {"query": "XR-7731", "k": 5}))
    assert [h["chunk_id"] for h in hits] == [
        public.chunk_id
    ]  # PHI chunk never surfaces
    assert hits[0]["snippet"].startswith("Acme Holdings")
    cleared = build_mcp_server(
        graph=RoutedGraph(),
        chunks=chunk_store,
        settings=settings.model_copy(update={"mcp_clearance": "phi"}),
    )
    assert {
        h["chunk_id"] for h in data(call(cleared, "nexo_search", {"query": "XR-7731"}))
    } == {public.chunk_id, phi.chunk_id}


def test_get_chunks_withholds_and_reports_missing_and_invalid(
    settings, chunk_store, stored
):
    """nexo_get_chunks returns visible text and lists withheld, missing and invalid ids."""
    public, phi = stored
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings
    )
    out = data(
        call(
            server,
            "nexo_get_chunks",
            {
                "chunk_ids": [
                    public.chunk_id,
                    phi.chunk_id,
                    "chk_" + "0" * 16,
                    "not-an-id",
                ]
            },
        )
    )
    assert [c["chunk_id"] for c in out["chunks"]] == [public.chunk_id]
    assert out["chunks"][0]["text"] == public.text
    assert out["withheld"] == [phi.chunk_id] and out["not_found"] == ["chk_" + "0" * 16]
    assert out["invalid_or_over_limit"] == ["not-an-id"]


def test_find_entities_is_parameterized_and_redacts(settings, chunk_store):
    """Names are bound as parameters, restricted entities redacted, unknown types rejected."""
    graph = RoutedGraph(
        {
            "MATCH (e:Entity)": [
                {
                    "entity": {
                        "key": "Organization:acme",
                        "name": "Acme",
                        "sensitivity_rank": 1,
                    }
                },
                {
                    "entity": {
                        "key": "Person:jane roe",
                        "name": "Jane Roe",
                        "sensitivity_rank": 3,
                    }
                },
            ]
        }
    )
    server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)
    out = data(
        call(
            server,
            "nexo_find_entities",
            {"name": "x' OR 1=1 //", "entity_type": "Organization"},
        )
    )
    assert out["entities"][0]["name"] == "Acme" and out["entities"][1] == {
        "redacted": "above caller clearance"
    }
    cypher, params = graph.calls[0]
    assert (
        "x' OR 1=1" not in cypher and params["name"] == "x' OR 1=1 //"
    )  # input never spliced into Cypher
    bad = call(
        server, "nexo_find_entities", {"name": "acme", "entity_type": "Spaceship"}
    )
    assert bad.is_error and "unknown entity type" in bad.content[0].text


def edge(
    relation: str, neighbor: str, neighbor_rank: int, edge_rank: int | None
) -> dict[str, Any]:
    """Build a neighbors row from Acme with the given neighbor and edge sensitivity."""
    return {
        "entity": {"key": "Organization:acme", "name": "Acme", "sensitivity_rank": 1},
        "relation": relation,
        "edge_rank": edge_rank,
        "outgoing": True,
        "predicate": None,
        "certainty": "asserted",
        "valid_at": None,
        "chunk_ids": ["chk_a"],
        "neighbor": {
            "key": f"X:{neighbor}",
            "name": neighbor,
            "type": "Organization",
            "sensitivity_rank": neighbor_rank,
        },
    }


def test_neighbors_hide_restricted_entities_and_edges(settings, chunk_store):
    """Restricted, PHI-sourced or unranked edges are withheld; a restricted root is an error."""
    rows = [
        edge("ACQUIRED", "Beta", 1, 1),
        edge("TREATS", "Jane", 3, 3),  # neighbor above clearance
        edge(
            "PAID", "Gamma", 1, 3
        ),  # visible neighbor, but only a PHI source states this edge
        edge("OWNS", "Delta", 1, None),
    ]  # edge without a rank: treated as most sensitive
    graph = RoutedGraph({"MATCH (e:Entity {key: $key})": rows})
    server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)
    out = data(call(server, "nexo_neighbors", {"entity_key": "Organization:acme"}))
    assert [e["neighbor"]["name"] for e in out["edges"]] == ["Beta"] and out[
        "withheld"
    ] == 3
    assert "edge_rank" not in out["edges"][0]
    restricted = RoutedGraph(
        {
            "MATCH (e:Entity {key: $key})": [
                {
                    **rows[0],
                    "entity": {
                        "key": "Person:jane",
                        "name": "Jane",
                        "sensitivity_rank": 3,
                    },
                }
            ]
        }
    )
    result = call(
        build_mcp_server(graph=restricted, chunks=chunk_store, settings=settings),
        "nexo_neighbors",
        {"entity_key": "Person:jane"},
    )
    assert result.is_error and "clearance" in result.content[0].text


def test_graph_query_rejects_writes_and_redacts_rows(settings, chunk_store):
    """Write Cypher never reaches the graph, and restricted nodes and their chunk ids are hidden."""
    graph = RoutedGraph(
        {
            "RETURN": [
                {
                    "e": {
                        "name": "Jane",
                        "sensitivity_rank": 3,
                        "chunk_id": "chk_" + "1" * 16,
                    },
                    "c": "chk_" + "2" * 16,
                }
            ]
        }
    )
    server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)
    rejected = call(server, "nexo_graph_query", {"cypher": "MATCH (n) DETACH DELETE n"})
    assert (
        rejected.is_error and "DETACH" in rejected.content[0].text and not graph.calls
    )
    out = data(
        call(
            server,
            "nexo_graph_query",
            {"cypher": "MATCH (e) RETURN e, e.chunk_id AS c LIMIT 1"},
        )
    )
    assert out["rows"][0]["e"] == {"redacted": "above caller clearance"}
    assert out["chunk_ids_found"] == [
        "chk_" + "2" * 16
    ]  # the redacted node's chunk id is not leaked


def test_unavailable_graph_is_a_clear_tool_error(settings, chunk_store):
    """A Neo4j outage surfaces as a tool error naming the unavailable graph."""
    server = build_mcp_server(
        graph=RoutedGraph(error=ServiceUnavailable("down")),
        chunks=chunk_store,
        settings=settings,
    )
    result = call(server, "nexo_index")
    assert result.is_error and "knowledge graph unavailable" in result.content[0].text


def test_stdout_carries_only_json_rpc_and_logs_go_to_stderr(tmp_path):
    """Strict clients reject any non-JSON line on stdout; the SDK client silently skips them."""
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    env = {
        "EMBEDDING_PROVIDER": "fake",
        "VECTOR_STORE_PATH": str(tmp_path / "v.json"),
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "LOG_LEVEL": "DEBUG",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "PATH": str(Path(sys.executable).parent),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "nexo.cli", "mcp"],
        input=json.dumps(initialize) + "\n",
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,  # the output is asserted below, not the exit status
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert lines, proc.stderr
    messages = [
        json.loads(line) for line in lines
    ]  # fails on any log line or stray print
    assert (
        messages[0]["id"] == 1 and messages[0]["result"]["serverInfo"]["name"] == "nexo"
    )
    assert "serving MCP over stdio" in proc.stderr


def test_nexo_mcp_speaks_clean_stdio(settings, tmp_path, stored):
    """Launch `nexo mcp` as a subprocess: logging must not touch stdout, or the handshake breaks."""
    env = {
        "EMBEDDING_PROVIDER": "fake",
        "EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
        "VECTOR_STORE_PATH": str(settings.vector_store_path),
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "LOG_LEVEL": "DEBUG",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "PATH": str(Path(sys.executable).parent),
    }
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nexo.cli", "mcp"],
        env=env,
        cwd=str(tmp_path),
    )

    async def run():
        async with Client(params) as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            hits = await client.call_tool("nexo_search", {"query": "XR-7731"})
            return tools, hits

    tools, hits = asyncio.run(run())
    assert tools == TOOLS
    assert [h["chunk_id"] for h in data(hits)] == [stored[0].chunk_id]
