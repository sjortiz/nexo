"""Runs the real Cypher against Neo4j. Skipped unless NEO4J_TEST_URI is set, e.g.

podman run -d -p 127.0.0.1:7688:7687 -e NEO4J_AUTH=neo4j/nexo-test-pass \
    neo4j:5.26-community
NEO4J_TEST_URI=bolt://127.0.0.1:7688 NEO4J_TEST_PASSWORD=nexo-test-pass \
    pytest tests/test_neo4j_integration.py
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from urllib.parse import urlparse

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError
from opentelemetry.trace import SpanKind
from strands.models.openai import OpenAIModel

from nexo.agents.investigator import Investigator
from nexo.agents.models import InvestigationScope
from nexo.agents.tools import InvestigationContext, build_investigation_tools
from nexo.mcp_server import build_mcp_server
from nexo.orchestration.graph import GraphDependencies, build_graph
from nexo.service import Runtime
from nexo.storage.graph_store import GraphStore, QueryLimits
from nexo.storage.ingest import (
    ExtractionAids,
    IngestionTargets,
    KnowledgeGraphIngestor,
)
from nexo.storage.resolution import EntityResolver
from nexo.storage.schemas import (
    ChunkRecord,
    DocumentMetadata,
    EntityRef,
    EntityType,
    GroundedTriple,
    Resolution,
    ResolvedEntity,
    Sensitivity,
    Triple,
)
from nexo.storage.sensitive_data import NoopDetector, RegexDetector
from nexo.storage.vector_store import ChunkFilter
from tests.conftest import RecordingChatModel
from tests.test_agent import ScriptedClient, _completion, _tool_call
from tests.test_mcp_server import call, data

URI = os.environ.get("NEO4J_TEST_URI")
pytestmark = pytest.mark.skipif(not URI, reason="NEO4J_TEST_URI not set")

C1, C2, C3 = ("chk_" + c * 16 for c in "123")


def _resolved(name, type_="Organization", key=None, method=Resolution.NEW, aliases=()):
    """Build a resolved entity, keyed by its own name unless `key` is given."""
    ref = EntityRef(name=name, type=type_, aliases=list(aliases))
    return ResolvedEntity(key=key or ref.key, surface=name, method=method, ref=ref)


def _row(chunk_id, source_id, edge, sensitivity=Sensitivity.INTERNAL, original=None):
    """Build a grounded triple for `edge` = (head, relation, tail) found in one chunk."""
    head, rel, tail = edge
    triple = Triple(
        head=head.ref,
        relation=rel,
        tail=tail.ref,
        evidence=f"{head.surface} {rel} {tail.surface}",
    )
    return GroundedTriple(
        triple=triple,
        chunk_id=chunk_id,
        source_id=source_id,
        sensitivity=sensitivity,
        relation=triple.relation,
        original_relation=original,
        head=head,
        tail=tail,
    )


def _chunks(source_id, *chunk_ids, sensitivity=Sensitivity.INTERNAL):
    """Build ordered chunk records for one source."""
    return [
        ChunkRecord(
            chunk_id=c,
            source_id=source_id,
            ordinal=i,
            text=f"text {i}",
            sensitivity=sensitivity,
            ingested_at=datetime.now(UTC),
        )
        for i, c in enumerate(chunk_ids)
    ]


@pytest.fixture(name="store")
def fixture_store():
    """A GraphStore on an emptied test database with the schema applied."""
    driver = GraphDatabase.driver(
        URI, auth=("neo4j", os.environ["NEO4J_TEST_PASSWORD"])
    )
    driver.execute_query("MATCH (n) DETACH DELETE n")
    graph = GraphStore(
        driver, database="neo4j", limits=QueryLimits(timeout_s=10, max_rows=50), uri=URI
    )
    graph.ensure_schema()
    yield graph
    graph.close()


def test_replace_source_merges_provenance_and_cleans_up(store):
    """Sources merge provenance on shared entities; withdrawing one removes only its data."""
    acme, beta = _resolved("Acme"), _resolved("Beta Labs")
    doc_a = [
        _row(C1, "a", (acme, "ACQUIRED", beta)),
        _row(
            C2, "a", (beta, "TREATS", _resolved("Jane Roe", "Person")), Sensitivity.PHI
        ),
    ]
    doc_b = [_row(C3, "b", (_resolved("acme"), "ACQUIRED", _resolved("beta labs")))]
    chunks_a = _chunks("a", C1, C2)
    chunks_a[1] = chunks_a[1].model_copy(
        update={"sensitivity": Sensitivity.PHI}
    )  # the chunk about Jane Roe
    assert store.replace_source(DocumentMetadata(source_id="a"), chunks_a, doc_a) == 2
    store.replace_source(DocumentMetadata(source_id="b"), _chunks("b", C3), doc_b)

    acme_row = store.run_read_query(
        "MATCH (e:Entity:Organization {key: 'Organization:acme'}) "
        "RETURN e.chunk_id AS first, e.chunk_ids AS ids"
    ).rows
    assert acme_row == [
        {"first": C1, "ids": [C1, C3]}
    ]  # same key across documents, provenance merged
    edge = store.run_read_query(
        "MATCH (:Entity)-[r:ACQUIRED]->(:Entity) RETURN r.chunk_ids AS ids"
    ).rows
    assert edge == [{"ids": [C1, C3]}]
    labels = store.run_read_query(
        "MATCH (e:Entity {name: 'Jane Roe'}) RETURN labels(e) AS l"
    ).rows[0]["l"]
    assert {"Entity", "Person", "PII", "PHI"} <= set(labels)
    hops = store.run_read_query(
        "MATCH p = (a:Entity {name: 'Acme'})-[:ACQUIRED|TREATS*..2]->"
        "(b:Entity {name: 'Jane Roe'}) "
        "RETURN length(p) AS n"
    ).rows
    assert hops == [{"n": 2}]

    store.replace_source(
        DocumentMetadata(source_id="a"), [], []
    )  # document a withdrawn
    remaining = store.run_read_query(
        "MATCH (e:Entity) RETURN e.name AS name, e.chunk_id AS first ORDER BY name"
    ).rows
    assert remaining == [
        {"name": "Acme", "first": C3},
        {"name": "Beta Labs", "first": C3},
    ]
    assert store.run_read_query(
        "MATCH (c:Chunk) RETURN collect(c.chunk_id) AS ids"
    ).rows == [{"ids": [C3]}]
    assert store.run_read_query(
        "MATCH (d:Document) RETURN collect(d.source_id) AS ids"
    ).rows == [{"ids": ["b"]}]


def test_lexical_graph_orders_chunks_and_links_document(store):
    """Chunks form an ordered NEXT_CHUNK chain linked to their Document."""
    store.replace_source(
        DocumentMetadata(source_id="a", title="Memo", doc_type="memo"),
        _chunks("a", C1, C2, C3),
        [],
    )
    path = store.run_read_query(
        "MATCH (d:Document {source_id: 'a'})-[:FIRST_CHUNK]->(first:Chunk) "
        "MATCH p = (first)-[:NEXT_CHUNK*0..]->(last:Chunk) WHERE NOT (last)-[:NEXT_CHUNK]->() "
        "RETURN [c IN nodes(p) | c.chunk_id] AS order, d.title AS title"
    ).rows
    assert path == [{"order": [C1, C2, C3], "title": "Memo"}]
    parts = store.run_read_query(
        "MATCH (:Chunk)-[:PART_OF]->(d:Document) RETURN count(*) AS n"
    ).rows
    assert parts == [{"n": 3}]


def test_aliases_follow_mentions_and_leave_with_the_document(store):
    """Aliases come from mentions, drive candidate lookup and leave with their source."""
    canonical = _resolved("Acme Holdings Inc.", key="Organization:acme holdings")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1),
        [_row(C1, "a", (canonical, "ACQUIRED", _resolved("Beta")))],
    )
    merged = _resolved(
        "AHI", key="Organization:acme holdings", method=Resolution.LLM, aliases=["Acme"]
    )
    store.replace_source(
        DocumentMetadata(source_id="b"),
        _chunks("b", C2),
        [_row(C2, "b", (merged, "SUPPLIES", _resolved("Gamma")))],
    )

    entity = store.run_read_query(
        "MATCH (e:Entity {key: 'Organization:acme holdings'}) "
        "RETURN e.name AS name, e.aliases AS aliases"
    ).rows
    assert entity == [{"name": "Acme Holdings Inc.", "aliases": ["AHI", "Acme"]}]
    how = store.run_read_query(
        "MATCH (:Entity {key: 'Organization:acme holdings'})-[m:MENTIONED_IN]->(c:Chunk) "
        "RETURN m.surface AS surface, m.resolution AS method, c.chunk_id AS chunk ORDER BY surface"
    ).rows
    assert how == [
        {"surface": "AHI", "method": "llm", "chunk": C2},
        {"surface": "Acme", "method": "stated_alias", "chunk": C2},
        {"surface": "Acme Holdings Inc.", "method": "new", "chunk": C1},
    ]
    candidates = store.find_candidates(EntityType.ORGANIZATION, ["ahi"], 10)
    assert [c.key for c in candidates] == [
        "Organization:acme holdings"
    ]  # found by alias
    assert (
        store.find_candidates(EntityType.PERSON, ["ahi"], 10) == []
    )  # never across types

    store.replace_source(
        DocumentMetadata(source_id="b"), [], []
    )  # the merging document is withdrawn
    entity = store.run_read_query(
        "MATCH (e:Entity {key: 'Organization:acme holdings'}) RETURN e.aliases AS aliases"
    ).rows
    assert entity == [{"aliases": []}]


def test_related_to_keeps_one_edge_per_predicate(store):
    """RELATED_TO keeps one edge per predicate; typed relations merge extracted predicates."""
    a, b = _resolved("A"), _resolved("B")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1),
        [
            _row(C1, "a", (a, "RELATED_TO", b), original="ADMIRES"),
            _row(C1, "a", (a, "RELATED_TO", b), original="SUES"),
            _row(C1, "a", (a, "ACQUIRED", b), original="BOUGHT"),
            _row(C1, "a", (a, "ACQUIRED", b), original="PURCHASED"),
        ],
    )
    edges = store.run_read_query(
        "MATCH (:Entity {name: 'A'})-[r]->(:Entity {name: 'B'}) "
        "RETURN type(r) AS t, r.predicate AS p, r.extracted_as AS seen ORDER BY t, p"
    ).rows
    assert edges == [
        {"t": "ACQUIRED", "p": None, "seen": ["BOUGHT", "PURCHASED"]},
        {"t": "RELATED_TO", "p": "ADMIRES", "seen": ["ADMIRES"]},
        {"t": "RELATED_TO", "p": "SUES", "seen": ["SUES"]},
    ]


def test_ingestion_resolves_across_documents_through_the_graph(
    store, settings, chunk_store
):
    """A later document's mention resolves to an existing entity via graph candidates."""

    def extraction(head, tail, evidence):
        return json.dumps(
            {
                "triples": [
                    {
                        "head": {
                            "name": head,
                            "type": "Organization",
                            "description": "holding co",
                        },
                        "relation": "acquired",
                        "tail": {"name": tail, "type": "Organization"},
                        "evidence": evidence,
                    }
                ]
            }
        )

    def ingest(text, source_id, replies):
        llm = RecordingChatModel(replies=replies)
        ingestor = KnowledgeGraphIngestor(
            llm=llm,
            detector=NoopDetector(),
            settings=settings,
            targets=IngestionTargets(chunk_store=chunk_store, graph_writer=store),
            aids=ExtractionAids(
                resolver=EntityResolver(catalog=store, llm=llm, mode="llm")
            ),
        )
        return ingestor.ingest(text, DocumentMetadata(source_id=source_id)), llm

    ingest(
        "# A\n\nAcme Holdings Inc. acquired Beta Labs.",
        "a",
        [
            extraction(
                "Acme Holdings Inc.",
                "Beta Labs",
                "Acme Holdings Inc. acquired Beta Labs",
            )
        ],
    )
    report, llm = ingest(
        "# B\n\nAcme Hldgs acquired Gamma Corp.",
        "b",
        [
            extraction("Acme Hldgs", "Gamma Corp", "Acme Hldgs acquired Gamma Corp"),
            json.dumps(
                {"decisions": [{"mention": 0, "match": "Organization:acme holdings"}]}
            ),
        ],
    )
    # The resolver was shown the existing entity, found by the real token-blocking query.
    assert "Organization:acme holdings" in llm.prompts[1][-1].content
    assert report.resolution["llm"] == 1
    acme = store.run_read_query(
        "MATCH (e:Entity {key: 'Organization:acme holdings'})-[r:ACQUIRED]->(t) "
        "WITH e, t ORDER BY t.name "
        "RETURN e.aliases AS aliases, collect(t.name) AS targets"
    ).rows
    assert acme == [{"aliases": ["Acme Hldgs"], "targets": ["Beta Labs", "Gamma Corp"]}]
    assert store.run_read_query(
        "MATCH (e:Entity) WHERE e.name STARTS WITH 'Acme' RETURN count(e) AS n"
    ).rows == [{"n": 1}]


def test_sensitivity_is_the_least_sensitive_source_and_follows_withdrawals(store):
    """Entity and edge sensitivity track the least sensitive live source, across withdrawals."""
    acme, beta = _resolved("Acme"), _resolved("Beta")

    def state():
        entity = store.run_read_query(
            "MATCH (e:Entity {name: 'Acme'}) RETURN e.sensitivity_rank AS rank, labels(e) AS labels"
        ).rows[0]
        edge = store.run_read_query(
            "MATCH (:Entity)-[r:ACQUIRED]->(:Entity) RETURN r.sensitivity_rank AS rank"
        ).rows
        return (
            entity["rank"],
            {"PII", "PHI"} & set(entity["labels"]),
            [e["rank"] for e in edge],
        )

    phi = Sensitivity.PHI
    store.replace_source(
        DocumentMetadata(source_id="clinic", sensitivity=phi),
        _chunks("clinic", C1, sensitivity=phi),
        [_row(C1, "clinic", (acme, "ACQUIRED", beta), phi)],
    )
    assert state() == (3, {"PII", "PHI"}, [3])  # only a PHI source: PHI

    store.replace_source(
        DocumentMetadata(source_id="memo"),
        _chunks("memo", C2),
        [_row(C2, "memo", (acme, "ACQUIRED", beta))],
    )
    assert state() == (
        1,
        set(),
        [1],
    )  # an internal source states it too: visible to internal callers

    store.replace_source(DocumentMetadata(source_id="memo"), [], [])  # memo withdrawn
    assert state() == (3, {"PII", "PHI"}, [3])  # back to PHI; labels return


def test_mcp_tools_against_a_real_graph(store, settings, chunk_store):
    """MCP tools list, find, traverse and query a real graph while withholding PHI."""
    acme, beta, jane = (
        _resolved("Acme Holdings"),
        _resolved("Beta Labs"),
        _resolved("Jane Roe", "Person"),
    )
    store.replace_source(
        DocumentMetadata(source_id="deals", title="Deals", doc_type="memo"),
        _chunks("deals", C1),
        [_row(C1, "deals", (acme, "ACQUIRED", beta))],
    )
    store.replace_source(
        DocumentMetadata(source_id="clinic", sensitivity=Sensitivity.PHI),
        _chunks("clinic", C2, sensitivity=Sensitivity.PHI),
        [_row(C2, "clinic", (beta, "TREATS", jane), Sensitivity.PHI)],
    )
    server = build_mcp_server(graph=store, chunks=chunk_store, settings=settings)

    index = data(call(server, "nexo_index"))
    assert [d["source_id"] for d in index["documents"]] == ["deals"] and index[
        "documents_withheld"
    ] == 1
    assert "ACQUIRED" in index["schema"]

    entities = data(call(server, "nexo_find_entities", {"name": "acme"}))["entities"]
    assert [e["key"] for e in entities] == ["Organization:acme holdings"]
    neighbors = data(
        call(server, "nexo_neighbors", {"entity_key": "Organization:beta labs"})
    )
    assert [
        (e["relation"], e["outgoing"], e["neighbor"]["name"])
        for e in neighbors["edges"]
    ] == [("ACQUIRED", False, "Acme Holdings")]
    assert neighbors["withheld"] == 1  # Jane Roe is PHI
    hidden = call(server, "nexo_neighbors", {"entity_key": "Person:jane roe"})
    assert hidden.is_error and "clearance" in hidden.content[0].text

    rows = data(
        call(
            server,
            "nexo_graph_query",
            {
                "cypher": "MATCH (a:Entity)-[r:ACQUIRED]->(b:Entity) "
                "RETURN a.name AS a, b.name AS b, r.chunk_id AS c"
            },
        )
    )
    assert rows["rows"] == [{"a": "Acme Holdings", "b": "Beta Labs", "c": C1}] and rows[
        "chunk_ids_found"
    ] == [C1]


def test_read_transaction_rejects_writes_server_side(store):
    """Neo4j refuses writes inside the read transaction even without the Cypher guard."""
    with pytest.raises(ClientError):
        store.run_read_query(
            "CREATE (:Entity {key: 'x'})"
        )  # guard bypassed: the server still refuses


def test_row_cap_tool_redaction_and_db_span(store, settings, spans):
    """Reads are row-capped, tool output is redacted above clearance and a DB span is emitted."""
    hub = _resolved("Hub")
    store.replace_source(
        DocumentMetadata(source_id="a"),
        _chunks("a", C1),
        [
            _row(C1, "a", (_resolved(f"Org{i}"), "PARTNERS_WITH", hub))
            for i in range(60)
        ],
    )
    assert store.run_read_query("MATCH (e:Entity) RETURN e.name").truncated is True
    ctx = InvestigationContext(
        request_id="r",
        max_tool_calls=5,
        scope=InvestigationScope(
            chunk_filter=ChunkFilter(max_sensitivity=Sensitivity.PUBLIC)
        ),
    )
    query, _ = build_investigation_tools(
        graph=store, chunks=None, ctx=ctx, settings=settings
    )
    spans.clear()
    out = json.loads(query("MATCH (e:Entity {name: 'Hub'}) RETURN e"))
    assert out["rows"] == [
        {"e": {"redacted": "above caller clearance"}}
    ]  # internal > public
    [db_span] = [s for s in spans.get_finished_spans() if s.name == "agent_query"]
    assert db_span.kind is SpanKind.CLIENT
    assert (
        db_span.attributes["db.system.name"] == "neo4j"
        and db_span.attributes["db.namespace"] == "neo4j"
    )
    assert db_span.attributes["server.address"] == urlparse(URI).hostname
    assert (
        "db.query.text" not in db_span.attributes
    )  # content capture is off by default
    assert (
        "(:Organization)-[:PARTNERS_WITH]->(:Organization)  x60"
        in store.describe_schema()
    )


def _last_tool_payload(request: dict) -> dict:
    """Decode the JSON tool result carried by the last message of a model request."""
    content = request["messages"][-1]["content"]
    return json.loads(content[0]["text"] if isinstance(content, list) else content)


def test_end_to_end_ingest_investigate_approve(store, settings, chunk_store):
    """Module A -> Neo4j/vector store -> Module C graph -> Module B Strands loop -> approval."""
    doc = (
        "# Deals\n\nAcme Holdings acquired Beta Labs in 2024. "
        "Beta Labs supplies Gamma Clinics."
    )
    extraction = {
        "triples": [
            {
                "head": {"name": "Acme Holdings", "type": "Organization"},
                "relation": "acquired",
                "tail": {"name": "Beta Labs", "type": "Organization"},
                "evidence": "Acme Holdings acquired Beta Labs",
            },
            {
                "head": {"name": "Beta Labs", "type": "Organization"},
                "relation": "supplies",
                "tail": {"name": "Gamma Clinics", "type": "Organization"},
                "evidence": "Beta Labs supplies Gamma Clinics",
            },
        ]
    }
    ingestor = KnowledgeGraphIngestor(
        llm=GenericFakeChatModel(
            messages=iter([AIMessage(content=json.dumps(extraction))])
        ),
        detector=RegexDetector(),
        settings=settings,
        targets=IngestionTargets(chunk_store=chunk_store, graph_writer=store),
    )
    report = ingestor.ingest(doc, DocumentMetadata(source_id="deals", title="Deals"))
    assert report.triples_written == 2
    [chunk_id] = (
        store.run_read_query("MATCH (c:Chunk) RETURN c.chunk_id AS id").rows[0].values()
    )

    cypher = (
        "MATCH p = (a:Entity {name: 'Acme Holdings'})-[*..3]->"
        "(c:Entity {name: 'Gamma Clinics'}) "
        "RETURN [n IN nodes(p) | n.name] AS path, "
        "[r IN relationships(p) | r.chunk_id] AS chunk_ids"
    )
    client = ScriptedClient(
        [
            _tool_call("t1", "query_knowledge_graph", {"cypher_query": cypher}),
            _tool_call("t2", "fetch_vector_chunks", {"chunk_ids": [chunk_id]}),
            _completion(
                {
                    "content": "Acme owns Beta Labs, which supplies Gamma Clinics "
                    f"[chunk:{chunk_id}]."
                },
                "stop",
            ),
        ]
    )
    investigator = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=store,
        chunks=chunk_store,
        settings=settings,
        schema_text=store.describe_schema(),
    )
    rt = Runtime(
        graph=build_graph(
            GraphDependencies(
                settings=settings, investigator=investigator, detector=RegexDetector()
            ),
            InMemorySaver(),
        ),
        settings=settings,
    )

    paused = rt.start(
        "How is Acme Holdings connected to Gamma Clinics?", thread_id="e2e"
    )
    assert paused.status == "awaiting_review"
    assert paused.pending_review is not None
    # dict(): pylint reads the field's default (None), not its declared type.
    review = dict(paused.pending_review)
    assert review["citations"] == [chunk_id]
    assert review["compliance_report"]["blocking"] is False
    assert _last_tool_payload(client.requests[1])["rows"][0]["path"] == [
        "Acme Holdings",
        "Beta Labs",
        "Gamma Clinics",
    ]

    done = rt.review("e2e", {"approval_status": "approved", "reviewer": "analyst"})
    assert done.final_response.startswith("Acme owns Beta Labs")
