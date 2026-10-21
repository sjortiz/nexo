"""Graph lookups against a real Neo4j: entity neighbours, paths and edge evidence
across clearances, and the warnings those queries emit.

Split from test_neo4j_integration.py to stay under pylint's module size.
"""

from __future__ import annotations

import json
import os
import re
import uuid

import pytest
from neo4j import GraphDatabase

from nexo.agents.investigator import SYSTEM_PROMPT
from nexo.agents.models import InvestigationScope
from nexo.agents.tools import (
    _NEIGHBOURS,
    _PATHS,
    _UNKNOWN_RANK,
    InvestigationContext,
    build_investigation_tools,
)
from nexo.mcp_server import _INDEX_DOCUMENTS, build_mcp_server
from nexo.storage.schemas import (
    Attribute,
    DocumentMetadata,
    GroundedAttribute,
    GroundedTriple,
    Sensitivity,
)
from nexo.storage.vector_store import ChunkFilter
from tests.conftest import as_model_text
from tests.test_mcp_server import call, data
from tests.test_neo4j_integration import (  # pylint: disable=unused-import
    C1,
    C2,
    C3,
    URI,
    _chunks,
    _resolved,
    _row,
    fixture_store,
)

pytestmark = pytest.mark.skipif(not URI, reason="NEO4J_TEST_URI not set")


def test_lookup_tools_follow_entity_edges_within_clearance(store, settings):
    """find_paths and entity_neighbours skip chunk co-mentions and hide PHI."""
    acme, beta, gamma = _resolved("Acme"), _resolved("Beta"), _resolved("Gamma")
    jane = _resolved("Jane Roe", "Person")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1, C2),
        [
            _row(C1, "a", (acme, "ACQUIRED", beta)),
            # Gamma shares C1 with Acme: a 2-hop route through the chunk.
            _row(C1, "a", (gamma, "PARTNERS_WITH", _resolved("Delta"))),
            _row(C2, "a", (beta, "SUPPLIES", gamma)),
        ],
    )
    store.replace_source(
        DocumentMetadata(source_id="p"),
        _chunks("p", C3, sensitivity=Sensitivity.PHI),
        [_row(C3, "p", (beta, "TREATS", jane), Sensitivity.PHI)],
    )
    ctx = InvestigationContext(
        request_id="r",
        max_tool_calls=10,
        scope=InvestigationScope(
            chunk_filter=ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
        ),
    )
    tools = {
        t.tool_name: as_model_text(t)
        for t in build_investigation_tools(
            graph=store, chunks=None, ctx=ctx, settings=settings
        )
    }

    out = json.loads(tools["find_paths"](acme.key, gamma.key))
    assert [
        [(h["from"], h["relation"], h["to"]) for h in p["hops"]] for p in out["paths"]
    ] == [[(acme.key, "ACQUIRED", beta.key), (beta.key, "SUPPLIES", gamma.key)]]
    assert out["chunk_ids_found"] == [C1, C2]

    # A hidden endpoint reads as unknown, so a path to it cannot reveal it exists.
    hidden = json.loads(tools["find_paths"](acme.key, jane.key))
    missing = json.loads(tools["find_paths"](acme.key, "Person:nobody"))
    assert (
        hidden["error"] == missing["error"] and "unknown entity key" in hidden["error"]
    )
    assert "unknown" in json.loads(tools["find_paths"](acme.key, "X:none"))["error"]

    out = json.loads(tools["entity_neighbours"](beta.key))
    assert sorted(
        (e["edge"]["relation"], e["edge"]["outgoing"], e["neighbour"]["name"])
        for e in out["edges"]
    ) == [("ACQUIRED", False, "Acme"), ("SUPPLIES", True, "Gamma")]
    assert out["withheld"] == 1 and C3 not in out["chunk_ids_found"]
    assert "unknown" in json.loads(tools["entity_neighbours"](jane.key))["error"]

    # An edge without a rank (written before ranks existed) is treated as PHI.
    with GraphDatabase.driver(
        URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
    ) as driver:
        driver.execute_query("MATCH ()-[r:SUPPLIES]->() REMOVE r.sensitivity_rank")
    out = json.loads(tools["find_paths"](acme.key, gamma.key))
    assert out["paths"] == [] and out["withheld"] == 1


def _warnings(cypher: str, parameters: dict) -> list[str]:
    """The GQL warning statuses (01xxx) Neo4j attaches to one query."""
    with (
        GraphDatabase.driver(
            URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
        ) as driver,
        driver.session(database="neo4j") as session,
    ):
        summary = session.run(cypher, parameters).consume()
    return [
        s.gql_status for s in summary.gql_status_objects if s.gql_status[:2] == "01"
    ]


def test_index_reads_optional_document_properties_without_warnings(
    store, settings, chunk_store, monkeypatch
):
    """An undated corpus lists null metadata without any 01xxx GQL status."""
    store.replace_source(DocumentMetadata(source_id="memo"), _chunks("memo", C1), [])
    with store.session(write=True) as session:
        session.run("MATCH (d:Document) REMOVE d.title, d.doc_type").consume()
    read = store.run_read_query
    queries = []

    def checked_read(cypher, parameters=None):
        queries.append(cypher)
        assert _warnings(cypher, parameters or {}) == []
        return read(cypher, parameters)

    monkeypatch.setattr(store, "run_read_query", checked_read)
    server = build_mcp_server(graph=store, chunks=chunk_store, settings=settings)
    assert data(call(server, "nexo_index"))["documents"] == [
        {
            "source_id": "memo",
            "title": None,
            "doc_type": None,
            "effective_date": None,
            "sensitivity": "internal",
            "chunks": 1,
        }
    ]
    # Property keys survive fixture deletion. Probe keys never seen by Neo4j
    # so earlier dated documents cannot hide a return to dot reads.
    listing = queries[0]
    unseen = f"never_{uuid.uuid4().hex}"
    assert _warnings(f"MATCH (d:Document) RETURN d.{unseen}", {}) == ["01N52"]
    for key in ("title", "doc_type", "effective_date"):
        assert _warnings(listing.replace(key, unseen + key), {}) == []


def test_investigator_time_example_is_warning_free_on_undated_graph(store):
    """The rendered prompt's actual Cypher handles absent dates and still finds dated facts."""
    acme, beta = _resolved("Acme"), _resolved("Beta")
    store.replace_source(
        DocumentMetadata(source_id="timeline"),
        _chunks("timeline", C1),
        [_row(C1, "timeline", (acme, "ACQUIRED", beta))],
    )
    prompt = SYSTEM_PROMPT.format(schema=store.describe_schema())
    example = re.search(r"- Facts over time[^\n]*:\n(.*?)\n- ", prompt, re.DOTALL)
    cypher = example.group(1).strip()
    assert _warnings(cypher, {}) == []
    assert store.run_read_query(cypher).rows == []
    assert _warnings(cypher.replace("valid_at", f"never_{uuid.uuid4().hex}"), {}) == []
    with store.session(write=True) as session:
        session.run("MATCH ()-[r:ACQUIRED]->() SET r.valid_at = '2025-02-01'").consume()
    assert store.run_read_query(cypher).rows == [
        {
            "a.name": "Acme",
            "type(r)": "ACQUIRED",
            "b.name": "Beta",
            "valid_at": "2025-02-01",
            "r.chunk_id": C1,
        }
    ]


def test_lookups_read_predicate_without_unknown_property_warnings(
    store, settings, chunk_store
):
    """No 01N52 warning on undated, non-RELATED_TO edges; RELATED_TO still returns its predicate."""
    acme, beta = _resolved("Acme"), _resolved("Beta")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1),
        [_row(C1, "a", (acme, "ACQUIRED", beta))],
    )
    reads = [
        (_NEIGHBOURS, {"key": acme.key, "limit": 25}),
        (
            _PATHS.format(paths=5, hops=3, unranked=_UNKNOWN_RANK),
            {"from_key": acme.key, "to_key": beta.key},
        ),
    ]
    # Earlier tests may have created these keys; keys Neo4j has never seen show
    # whether the queries would warn on a graph that never had them.
    unseen = f"never_{uuid.uuid4().hex}"
    assert _warnings(f"MATCH ()-[r]->() RETURN r.{unseen}", {}) == ["01N52"]
    for cypher, parameters in reads:
        assert _warnings(cypher, parameters) == []
        for key in ("predicate", "valid_at", "certainty"):
            assert _warnings(cypher.replace(key, unseen + key), parameters) == []

    store.replace_source(
        DocumentMetadata(source_id="b"),
        _chunks("b", C2),
        [_row(C2, "b", (acme, "RELATED_TO", beta), original="BANKROLLED")],
    )
    ctx = InvestigationContext(
        request_id="r",
        max_tool_calls=10,
        scope=InvestigationScope(
            chunk_filter=ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
        ),
    )
    tools = {
        t.tool_name: as_model_text(t)
        for t in build_investigation_tools(
            graph=store, chunks=None, ctx=ctx, settings=settings
        )
    }
    expected = [("ACQUIRED", None), ("RELATED_TO", "BANKROLLED")]
    out = json.loads(tools["entity_neighbours"](acme.key))
    assert (
        sorted((e["edge"]["relation"], e["edge"]["predicate"]) for e in out["edges"])
        == expected
    )
    out = json.loads(tools["find_paths"](acme.key, beta.key))
    assert (
        sorted(
            (p["hops"][0]["relation"], p["hops"][0]["predicate"]) for p in out["paths"]
        )
        == expected
    )
    out = data(
        call(
            build_mcp_server(graph=store, chunks=chunk_store, settings=settings),
            "nexo_neighbors",
            {"entity_key": acme.key},
        )
    )
    assert sorted((e["relation"], e["predicate"]) for e in out["edges"]) == expected
    for cypher, parameters in reads:
        assert _warnings(cypher, parameters) == []


def test_mcp_find_paths_against_a_real_graph(store, settings, chunk_store):
    """nexo_find_paths skips co-mentions, withholds PHI and unranked hops, hides PHI keys."""
    acme, beta, gamma = _resolved("Acme"), _resolved("Beta"), _resolved("Gamma")
    omega, jane = _resolved("Omega"), _resolved("Jane Roe", "Person")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1, C2),
        [
            _row(C1, "a", (acme, "ACQUIRED", beta)),
            # Omega shares C1 with Acme, but a chunk is never a link.
            _row(C1, "a", (omega, "PARTNERS_WITH", _resolved("Sigma"))),
            _row(C2, "a", (beta, "SUPPLIES", gamma)),
        ],
    )
    store.replace_source(
        DocumentMetadata(source_id="p", sensitivity=Sensitivity.PHI),
        _chunks("p", C3, sensitivity=Sensitivity.PHI),
        [
            _row(C3, "p", (acme, "EMPLOYS", jane), Sensitivity.PHI),
            _row(C3, "p", (jane, "OWNS", gamma), Sensitivity.PHI),
        ],
    )
    server = build_mcp_server(graph=store, chunks=chunk_store, settings=settings)

    def paths(from_key, to_key):
        return call(server, "nexo_find_paths", {"from_key": from_key, "to_key": to_key})

    out = data(paths(acme.key, gamma.key))
    assert [
        [(h["from"], h["relation"], h["to"]) for h in p["hops"]] for p in out["paths"]
    ] == [[(acme.key, "ACQUIRED", beta.key), (beta.key, "SUPPLIES", gamma.key)]]
    assert out["withheld"] == 1 and out["chunk_ids_found"] == [C1, C2]

    out = data(paths(acme.key, omega.key))
    assert out["paths"] == [] and out["withheld"] == 0 and "hint" in out
    hidden, unknown = paths(acme.key, jane.key), paths(acme.key, "X:none")
    assert hidden.is_error and unknown.is_error
    assert hidden.content[0].text == unknown.content[0].text

    # An edge without a rank (written before ranks existed) is treated as PHI.
    with GraphDatabase.driver(
        URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
    ) as driver:
        driver.execute_query("MATCH ()-[r:SUPPLIES]->() REMOVE r.sensitivity_rank")
    out = data(paths(acme.key, gamma.key))
    assert out["paths"] == [] and out["withheld"] == 2


def _quoting(row: GroundedTriple, evidence: str) -> GroundedTriple:
    """The same grounded triple with its own verbatim evidence."""
    return row.model_copy(
        update={"triple": row.triple.model_copy(update={"evidence": evidence})}
    )


def _edge_state(store) -> dict:
    """The ACQUIRED edge's quote, its chunk and both ranks, as stored."""
    return store.run_read_query(
        "MATCH (:Entity)-[r:ACQUIRED]->(:Entity) RETURN r.evidence AS evidence, "
        "r.evidence_rank AS evidence_rank, r.chunk_id AS chunk, "
        "r.sensitivity_rank AS rank"
    ).rows[0]


def test_evidence_shows_only_within_the_rank_of_the_chunk_it_quotes(
    store, settings, chunk_store
):
    """A PHI note then an internal memo state one edge: the edge is internal, its quote PHI."""
    acme, beta = _resolved("Acme"), _resolved("Beta")
    note = "Jane Roe's ward notes: Acme acquired Beta"
    phi = Sensitivity.PHI
    store.replace_source(
        DocumentMetadata(source_id="clinic", sensitivity=phi),
        _chunks("clinic", C1, sensitivity=phi),
        [_quoting(_row(C1, "clinic", (acme, "ACQUIRED", beta), phi), note)],
    )
    store.replace_source(
        DocumentMetadata(source_id="memo"),
        _chunks("memo", C2),
        [_quoting(_row(C2, "memo", (acme, "ACQUIRED", beta)), "Acme acquired Beta")],
    )
    assert _edge_state(store) == {
        "evidence": note,
        "evidence_rank": 3,
        "chunk": C1,
        "rank": 1,
    }

    def surfaces(clearance: Sensitivity) -> list[dict]:
        """Every read surface that returns edge maps, at one clearance."""
        mcp = build_mcp_server(
            graph=store,
            chunks=chunk_store,
            settings=settings.model_copy(update={"mcp_clearance": clearance.value}),
        )
        ctx = InvestigationContext(
            request_id="r",
            max_tool_calls=10,
            scope=InvestigationScope(
                chunk_filter=ChunkFilter(max_sensitivity=clearance)
            ),
        )
        agent = {
            t.tool_name: as_model_text(t)
            for t in build_investigation_tools(
                graph=store, chunks=None, ctx=ctx, settings=settings
            )
        }
        paths = data(
            call(mcp, "nexo_find_paths", {"from_key": acme.key, "to_key": beta.key})
        )
        query = data(
            call(
                mcp,
                "nexo_graph_query",
                # Every property; created_at as text, the MCP layer cannot encode a DateTime.
                {
                    "cypher": "MATCH (:Entity)-[r:ACQUIRED]->(:Entity) "
                    "RETURN r {.*, created_at: toString(r.created_at)} AS r"
                },
            )
        )
        found = json.loads(agent["find_paths"](acme.key, beta.key))
        around = json.loads(agent["entity_neighbours"](acme.key))
        assert paths["path_count"] == found["path_count"] == around["edge_count"] == 1
        return [
            paths["paths"][0]["hops"][0],
            query["rows"][0]["r"],
            found["paths"][0]["hops"][0],
            around["edges"][0]["edge"],
        ]

    for edge in surfaces(Sensitivity.INTERNAL):  # the edge is visible, its quote is not
        assert "evidence" not in edge and note not in json.dumps(edge)
    assert [e["evidence"] for e in surfaces(phi)] == [note] * 4

    # An edge written before evidence_rank existed shows no evidence to anyone.
    with GraphDatabase.driver(
        URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
    ) as driver:
        driver.execute_query("MATCH ()-[r:ACQUIRED]->() REMOVE r.evidence_rank")
    assert all("evidence" not in e for e in surfaces(phi))


def test_a_quote_leaves_with_its_chunk_and_the_next_source_requotes(store):
    """Re-ingesting, withdrawing or upgrading a source re-derives the quote and its rank."""
    acme, beta = _resolved("Acme"), _resolved("Beta")

    def ingest(source_id, chunk_id, evidence, sensitivity=Sensitivity.INTERNAL):
        store.replace_source(
            DocumentMetadata(source_id=source_id, sensitivity=sensitivity),
            _chunks(source_id, chunk_id, sensitivity=sensitivity),
            [
                _quoting(
                    _row(chunk_id, source_id, (acme, "ACQUIRED", beta), sensitivity),
                    evidence,
                )
            ],
        )

    ingest("memo", C1, "memo quote")
    ingest("notes", C2, "notes quote")
    assert _edge_state(store) == {
        "evidence": "memo quote",
        "evidence_rank": 1,
        "chunk": C1,
        "rank": 1,
    }
    # The memo is reclassified as PHI: its quote must not keep the internal rank.
    ingest("memo", C1, "memo quote, now PHI", Sensitivity.PHI)
    assert _edge_state(store) == {
        "evidence": "memo quote, now PHI",
        "evidence_rank": 3,
        "chunk": C1,
        "rank": 1,
    }
    store.replace_source(DocumentMetadata(source_id="memo"), [], [])  # withdrawn
    assert _edge_state(store) == {
        "evidence": None,
        "evidence_rank": None,
        "chunk": C2,
        "rank": 1,
    }
    ingest("notes", C2, "notes quote")
    assert _edge_state(store)["evidence"] == "notes quote"

    # A legacy edge (no evidence_rank) is re-quoted the next time a source states it.
    with GraphDatabase.driver(
        URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
    ) as driver:
        driver.execute_query("MATCH ()-[r:ACQUIRED]->() REMOVE r.evidence_rank")
    ingest("memo", C1, "memo quote again")
    assert _edge_state(store) == {
        "evidence": "memo quote again",
        "evidence_rank": 1,
        "chunk": C1,
        "rank": 1,
    }


def test_index_reads_undated_documents_without_unknown_property_warnings(store):
    """nexo_index reads title, doc_type and effective_date through properties(d)."""
    acme, beta = _resolved("Acme"), _resolved("Beta")
    store.replace_source(  # no title, type or date: the properties are never written
        DocumentMetadata(source_id="undated"),
        _chunks("undated", C1),
        [_row(C1, "undated", (acme, "ACQUIRED", beta))],
    )
    unseen = f"never_{uuid.uuid4().hex}"
    assert _warnings(f"MATCH (d:Document) RETURN d.{unseen}", {}) == ["01N52"]
    assert _warnings(_INDEX_DOCUMENTS, {}) == []
    for key in ("title", "doc_type", "effective_date"):
        assert _warnings(_INDEX_DOCUMENTS.replace(key, unseen + key), {}) == []


def test_attributes_ride_on_mentions_and_leave_with_the_document(store):
    """An attribute is stored on its MENTIONED_IN edge; re-ingesting without it removes it."""
    clinic = _resolved("Gamma Clinics")
    attribute = GroundedAttribute(
        attribute=Attribute(
            entity=clinic.ref, name="rating", value="4.8 stars", evidence="4.8 stars"
        ),
        chunk_id=C1,
        source_id="shops",
        entity=clinic,
    )

    def stored():
        with store.session(write=False) as session:
            return [
                json.loads(r["a"]) if r["a"] else None
                for r in session.run(
                    "MATCH (:Entity {key: $key})-[m:MENTIONED_IN]->(:Chunk)"
                    " RETURN m.attributes_json AS a",
                    key=clinic.key,
                )
            ]

    store.replace_source(
        DocumentMetadata(source_id="shops"), _chunks("shops", C1), [], [attribute]
    )
    assert stored() == [
        [{"evidence": "4.8 stars", "name": "rating", "value": "4.8 stars"}]
    ]
    store.replace_source(DocumentMetadata(source_id="shops"), _chunks("shops", C1), [])
    assert not stored()  # the mention, and the entity it alone supported, are gone
