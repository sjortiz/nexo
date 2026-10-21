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

from pydantic import SecretStr
from nexo.agents.redaction import ChunkRedactor
from nexo.agents.tools import redact_above_clearance
from nexo.telemetry import SESSION_ID, query_attributes
from nexo.mcp_server import build_mcp_server
from nexo.storage.graph_store import ReadResult
from nexo.storage.schemas import Sensitivity
from nexo.storage.sensitive_data import RegexDetector
from nexo.storage.vector_store import LocalChunkStore
from nexo.llm import build_embeddings
from tests.conftest import make_chunk
from tests.test_graph import FakeInvestigator, runtime

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
    assert out["masked"] == []
    assert out["invalid_or_over_limit"] == ["not-an-id"]


EMAIL = "jane.roe@beta.example"
CONTACT = f"Gamma Works signed with Beta Labs. Investor contact: {EMAIL}"


@pytest.fixture(name="contact")
def fixture_contact(chunk_store):
    """Store chunks above an internal clearance: one only for a detected email
    (maskable), one with a PHI span and one whose document is declared pii (both
    withheld, though each also holds the maskable email)."""
    pii = make_chunk(
        "board", 0, CONTACT, Sensitivity.PII, declared_sensitivity=Sensitivity.INTERNAL
    )
    mrn = make_chunk(
        "board",
        1,
        f"Gamma Works patient MRN: A1234567, {EMAIL}",
        Sensitivity.PHI,
        declared_sensitivity=Sensitivity.INTERNAL,
    )
    declared = make_chunk(
        "hr",
        0,
        f"Gamma Works salary review for Jane Roe, {EMAIL}",
        Sensitivity.PII,
        declared_sensitivity=Sensitivity.PII,
    )
    chunk_store.upsert([pii, mrn, declared], [pii.text, mrn.text, declared.text])
    return pii, mrn, declared


def _masking(settings, chunk_store, clearance="internal"):
    return build_mcp_server(
        graph=RoutedGraph(),
        chunks=chunk_store,
        settings=settings.model_copy(update={"mcp_clearance": clearance}),
        redactor=ChunkRedactor(RegexDetector()),
    )


def test_read_tools_mask_detected_pii_below_its_clearance(
    settings, chunk_store, contact
):
    """Search and get_chunks show the pii chunk with [EMAIL]; PHI and declared pii stay withheld."""
    pii, mrn, declared = contact
    server = _masking(settings, chunk_store)
    ids = [pii.chunk_id, mrn.chunk_id, declared.chunk_id]

    out = data(call(server, "nexo_get_chunks", {"chunk_ids": ids}))
    [shown] = out["chunks"]
    assert shown["chunk_id"] == pii.chunk_id and EMAIL not in shown["text"]
    assert shown["text"].endswith("Investor contact: [EMAIL]")
    assert out["masked"] == [pii.chunk_id]
    assert out["withheld"] == sorted([mrn.chunk_id, declared.chunk_id])

    hits = data(call(server, "nexo_search", {"query": "Gamma Works", "k": 5}))
    assert [h["chunk_id"] for h in hits] == [pii.chunk_id]
    assert hits[0]["snippet"].endswith("[EMAIL]") and "@" not in hits[0]["snippet"]


def test_search_masks_the_query_so_it_cannot_probe(settings, tmp_path, contact):
    """Searching a keyword index for the address finds nothing below its clearance."""
    keyword = LocalChunkStore(
        build_embeddings(settings), tmp_path / "keyword.json", mode="keyword"
    )
    keyword.upsert([contact[0]], [CONTACT])
    hits = data(call(_masking(settings, keyword), "nexo_search", {"query": EMAIL}))
    assert hits == []
    cleared = data(
        call(_masking(settings, keyword, "pii"), "nexo_search", {"query": EMAIL})
    )
    assert [h["chunk_id"] for h in cleared] == [contact[0].chunk_id]


PHONE_TEXT = "Escrow agent for Delta Partners, phone (415) 555-0134."


@pytest.fixture(name="fragments")
def fixture_fragments(settings, tmp_path, request, contact):
    """A store of the given mode holding the email chunk and a phone chunk, both maskable."""
    phone = make_chunk(
        "escrow",
        0,
        PHONE_TEXT,
        Sensitivity.PII,
        declared_sensitivity=Sensitivity.INTERNAL,
    )
    store = LocalChunkStore(
        build_embeddings(settings), tmp_path / "fragments.json", mode=request.param
    )
    store.upsert([contact[0], phone], [CONTACT, PHONE_TEXT])
    return store, contact[0], phone


@pytest.mark.parametrize("fragments", ["keyword", "hybrid", "vector"], indirect=True)
@pytest.mark.parametrize(
    "query",
    [
        "0134",
        "555-0134",
        "415",
        "(415) 555",
        "jane",
        "roe",
        "Delta 0134",
        "Jean Rowe",
        "email",
    ],
)
def test_search_does_not_match_fragments_of_a_masked_value(settings, fragments, query):
    """A query that matches a masked chunk only through its masked-out words finds nothing."""
    store, _, _ = fragments
    assert data(call(_masking(settings, store), "nexo_search", {"query": query})) == []


@pytest.mark.parametrize("fragments", ["keyword", "hybrid", "vector"], indirect=True)
def test_search_still_finds_a_masked_chunk_by_its_other_words(settings, fragments):
    """Other words find the masked chunk; a cleared caller can search the value itself."""
    store, email, phone = fragments
    server = _masking(settings, store)
    [hit] = data(call(server, "nexo_search", {"query": "Delta Partners", "k": 1}))
    assert hit["chunk_id"] == phone.chunk_id and hit["snippet"].endswith("[PHONE].")
    [hit] = data(call(server, "nexo_search", {"query": "Investor contact", "k": 1}))
    assert hit["chunk_id"] == email.chunk_id and hit["snippet"].endswith("[EMAIL]")
    cleared = _masking(settings, store, "pii")
    hits = data(call(cleared, "nexo_search", {"query": "0134", "k": 1}))
    assert [(h["chunk_id"], h["snippet"]) for h in hits] == [
        (phone.chunk_id, PHONE_TEXT)
    ]


@pytest.mark.parametrize("clearance", ["pii", "phi"])
def test_read_tools_show_the_original_to_a_sufficient_clearance(
    settings, chunk_store, contact, clearance
):
    """At or above the chunk's level nothing is masked."""
    pii = contact[0]
    server = _masking(settings, chunk_store, clearance)
    out = data(call(server, "nexo_get_chunks", {"chunk_ids": [pii.chunk_id]}))
    assert out["chunks"][0]["text"] == CONTACT and out["masked"] == []
    hits = data(call(server, "nexo_search", {"query": "Investor contact", "k": 5}))
    assert {h["chunk_id"]: h["snippet"] for h in hits}[pii.chunk_id] == CONTACT


def test_read_tools_withhold_whole_without_a_detector(settings, chunk_store, contact):
    """No redactor and no runtime: chunks above clearance are withheld, as before."""
    pii = contact[0]
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings
    )
    out = data(call(server, "nexo_get_chunks", {"chunk_ids": [pii.chunk_id]}))
    assert out["chunks"] == [] and out["withheld"] == [pii.chunk_id]
    assert data(call(server, "nexo_search", {"query": "Gamma Works"})) == []


def test_read_tools_use_the_runtimes_redactor(settings, chunk_store, contact):
    """With the review runtime, the read tools mask with the same redactor as the review."""
    pii = contact[0]
    rt = runtime(settings, FakeInvestigator([]))
    rt.chunks, rt.redactor = chunk_store, ChunkRedactor(RegexDetector())
    server = build_mcp_server(
        graph=RoutedGraph(), chunks=chunk_store, settings=settings, runtime=rt
    )
    out = data(call(server, "nexo_get_chunks", {"chunk_ids": [pii.chunk_id]}))
    assert out["masked"] == [pii.chunk_id] and "[EMAIL]" in out["chunks"][0]["text"]


CHUNK_NODE = {
    "chunk_id": "chk_" + "3" * 16,
    "text": CONTACT,
    "search_text": CONTACT,
    "embedding": [0.1, 0.2],
    "sensitivity": "pii",
}


@pytest.mark.parametrize("clearance", ["internal", "phi"])
def test_graph_query_never_returns_chunk_text(settings, chunk_store, clearance):
    """Chunk nodes are ranked by their level, and their text never leaves through Cypher."""
    entity = {"key": "Person:jane", "name": "Jane", "sensitivity_rank": 1}
    graph = RoutedGraph(
        {
            "RETURN": [
                {
                    "c": CHUNK_NODE,
                    "m": (entity, "MENTIONED_IN", CHUNK_NODE),
                    "mention": {"surface": EMAIL, "resolution": "exact"},
                    "public": {**CHUNK_NODE, "sensitivity": "public"},
                    "odd": {"name": "x", "sensitivity": "secret"},
                }
            ]
        }
    )
    server = build_mcp_server(
        graph=graph,
        chunks=chunk_store,
        settings=settings.model_copy(update={"mcp_clearance": clearance}),
    )
    out = data(
        call(
            server,
            "nexo_graph_query",
            {"cypher": "MATCH (e)-[m:MENTIONED_IN]->(c:Chunk) RETURN c, m LIMIT 1"},
        )
    )
    [row] = out["rows"]
    assert row["public"] == {
        "chunk_id": CHUNK_NODE["chunk_id"],
        "sensitivity": "public",
    }
    assert row["m"][0] == entity and row["m"][1] == "MENTIONED_IN"
    redacted = {"redacted": "above caller clearance"}
    if clearance == "internal":
        assert row["c"] == row["m"][2] == row["mention"] == row["odd"] == redacted
    else:
        assert row["c"] == row["m"][2] == row["public"] | {"sensitivity": "pii"}
        # A level that cannot be read counts as phi, like an unranked mention.
        assert row["mention"]["surface"] == EMAIL and row["odd"]["name"] == "x"
    assert CONTACT not in json.dumps(out) and "0.1" not in json.dumps(out)
    if clearance == "internal":
        assert EMAIL not in json.dumps(out)


def test_relationship_tuples_from_the_driver_are_redacted_too():
    """The driver returns a relationship as a (start, type, end) tuple; its nodes are checked."""
    entity = {"key": "Person:jane", "sensitivity_rank": 1}
    row = {"m": (entity, "MENTIONED_IN", CHUNK_NODE)}
    assert redact_above_clearance(row, Sensitivity.INTERNAL.rank) == {
        "m": [entity, "MENTIONED_IN", {"redacted": "above caller clearance"}]
    }


@pytest.mark.parametrize(
    "cypher",
    [
        "MATCH (c:Chunk) RETURN c.text AS t LIMIT 1",
        "MATCH (c) RETURN c . search_text LIMIT 1",
        "MATCH (c) RETURN c.`embedding` LIMIT 1",
        "MATCH (c) RETURN c['text'] LIMIT 1",
    ],
)
def test_graph_query_rejects_reading_chunk_text_by_name(settings, chunk_store, cypher):
    """Scalar reads of chunk text are refused before they reach the graph."""
    graph = RoutedGraph()
    server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)
    result = call(server, "nexo_graph_query", {"cypher": cypher})
    assert result.is_error and "chunk text" in result.content[0].text
    assert not graph.calls


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


ACME = {"key": "Organization:acme", "name": "Acme", "type": "Organization"}


def edge(relation: str, neighbor: str, neighbor_rank: int, edge_rank: int) -> dict:
    """A row of the shared neighbour query from Acme, ranks as given."""
    return {
        "entity": {**ACME, "sensitivity_rank": 1},
        "edge": {
            "relation": relation,
            "outgoing": True,
            "predicate": None,
            "certainty": "asserted",
            "valid_at": None,
            "evidence": "Acme ...",
            "chunk_ids": ["chk_a"],
            "sensitivity_rank": edge_rank,  # the query reads a missing rank as 3
        },
        "neighbour": {
            "key": f"X:{neighbor}",
            "name": neighbor,
            "type": "Organization",
            "sensitivity_rank": neighbor_rank,
        },
    }


def test_neighbors_hide_restricted_entities_and_edges(settings, chunk_store):
    """Edges above clearance or to hidden neighbors are withheld; a restricted root is an error."""
    rows = [
        edge("ACQUIRED", "Beta", 1, 1),
        edge("TREATS", "Jane", 3, 3),  # neighbor above clearance
        edge("PAID", "Gamma", 1, 3),  # only a PHI source states this edge
    ]
    graph = RoutedGraph({"MATCH (e:Entity {key: $key})": rows})
    server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)
    out = data(call(server, "nexo_neighbors", {"entity_key": "Organization:acme"}))
    assert out["edges"] == [
        {
            "relation": "ACQUIRED",
            "outgoing": True,
            "predicate": None,
            "certainty": "asserted",
            "valid_at": None,
            "chunk_ids": ["chk_a"],
            "neighbor": {**rows[0]["neighbour"]},
        }
    ]
    assert out["withheld"] == 2 and out["entity_key"] == "Organization:acme"
    assert graph.calls[0][1] == {"key": "Organization:acme", "limit": 25}

    jane = {"key": "Person:jane", "name": "Jane", "type": "Person"}
    restricted = RoutedGraph(
        {
            "MATCH (e:Entity {key: $key})": [
                {
                    "entity": {**jane, "sensitivity_rank": 3},
                    "edge": None,
                    "neighbour": None,
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
    unknown = data(
        call(
            build_mcp_server(
                graph=RoutedGraph(), chunks=chunk_store, settings=settings
            ),
            "nexo_neighbors",
            {"entity_key": "X:none"},
        )
    )
    assert unknown["edges"] == [] and unknown["withheld"] == 0


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
        "NEXO_DATA_DIR": str(tmp_path / "data"),  # never the user's review queue
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


@pytest.mark.parametrize("review", [True, False])
def test_nexo_mcp_speaks_clean_stdio(  # pylint: disable=too-many-arguments
    settings, tmp_path, stored, contact, review
):
    """Launch `nexo mcp` as a subprocess: logging must not touch stdout, or the handshake
    breaks; it masks with the configured detector whether or not the review tools are on.
    """
    env = {
        "EMBEDDING_PROVIDER": "fake",
        "EMBEDDING_DIMENSIONS": str(settings.embedding_dimensions),
        "CHUNK_STORE": "local",
        "VECTOR_STORE_PATH": str(settings.vector_store_path),
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "MCP_REVIEW_TOOLS": str(review).lower(),
        "CHECKPOINT_SQLITE_PATH": str(tmp_path / "checkpoints.sqlite"),
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
            read = await client.call_tool(
                "nexo_get_chunks", {"chunk_ids": [contact[0].chunk_id]}
            )
            return tools, hits, read

    tools, hits, read = asyncio.run(run())
    assert data(read)["chunks"][0]["text"].endswith("[EMAIL]")
    review_tools = {"nexo_pending_reviews", "nexo_get_review", "nexo_submit_review"}
    assert tools == (TOOLS | review_tools if review else TOOLS)
    found = [h["chunk_id"] for h in data(hits)]  # the masked contact chunk ranks lower
    assert found[0] == stored[0].chunk_id and stored[1].chunk_id not in found


def test_tool_spans_say_who_called_and_which_question(settings, chunk_store, spans):
    """Every MCP tool span has user.id and session.id; nexo_search adds the query hash."""
    keyed = settings.model_copy(
        update={"query_hash_key": SecretStr("k1"), "nexo_user": "claude-user"}
    )
    server = build_mcp_server(graph=RoutedGraph(), chunks=chunk_store, settings=keyed)
    call(server, "nexo_search", {"query": "XR-7731 order"})
    call(server, "nexo_index")
    finished = {s.name: s for s in spans.get_finished_spans()}
    search = finished["execute_tool nexo_search"]
    assert search.attributes["user.id"] == "claude-user"
    assert search.attributes["session.id"] == SESSION_ID
    assert (
        search.attributes["nexo.query.hash"]
        == query_attributes(keyed, "XR-7731 order")["nexo.query.hash"]
    )
    assert "XR-7731 order" not in str(dict(search.attributes))
    assert finished["execute_tool nexo_index"].attributes["user.id"] == "claude-user"
