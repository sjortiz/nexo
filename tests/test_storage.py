"""Storage primitives: schemas, chunking, sensitive-data detection, Cypher guard, chunk store."""

from __future__ import annotations

from datetime import date

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from neo4j.exceptions import Neo4jError
from pydantic import ValidationError

from nexo.llm import build_embeddings
from nexo.storage.chunking import (
    flatten_markdown_tables,
    glossary_notes,
    lint_document,
    split_document,
)
from nexo.storage.neo4j_chunk_store import (
    _PROCEDURES,
    _SEARCH_CLAUSE,
    Neo4jChunkStore,
    index_queries,
)
from nexo.storage.graph_store import (
    _RANK,
    UnsafeCypherError,
    _plan_writes,
    validate_read_only_cypher,
)
from nexo.storage.schemas import (
    DocumentMetadata,
    EntityRef,
    EntityType,
    ExtractionResult,
    GroundedTriple,
    Resolution,
    ResolvedEntity,
    Sensitivity,
    Triple,
    entity_key,
    normalize_relation,
)
from nexo.storage.sensitive_data import RegexDetector, classify, redact
from nexo.storage.vector_store import ChunkFilter, LocalChunkStore
from tests.conftest import make_chunk


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("works for", "WORKS_FOR"),
        ("worksFor", "WORKS_FOR"),
        ("WORKS-FOR", "WORKS_FOR"),
        ("3rd party of", "R_3RD_PARTY_OF"),
    ],
)
def test_normalize_relation(raw, expected):
    """Free-form relation labels normalize to UPPER_SNAKE identifiers."""
    assert normalize_relation(raw) == expected


def test_triple_rejects_relation_that_cannot_be_normalized():
    """A relation with no usable characters fails validation."""
    with pytest.raises(ValidationError):
        Triple(
            head=EntityRef(name="A", type="Person"),
            relation="!!!",
            tail=EntityRef(name="B", type="Person"),
            evidence="x",
        )


def test_extraction_schema_forbids_unknown_fields_and_types():
    """Extraction output rejects extra fields and unknown entity types."""
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate({"triples": [], "notes": "x"})
    with pytest.raises(ValidationError):
        EntityRef(name="X", type="Spaceship")


def test_entity_key_folds_case_and_whitespace():
    """Entity keys ignore case and spacing but distinguish entity types."""
    assert entity_key("  Acme   Corp ", EntityType.ORGANIZATION) == entity_key(
        "acme corp", EntityType.ORGANIZATION
    )
    assert entity_key("Acme", EntityType.ORGANIZATION) != entity_key(
        "Acme", EntityType.PRODUCT
    )


def test_flatten_tables_and_lint():
    """Markdown tables flatten to row bullets and lint flags documents without headings."""
    text = (
        "# Drugs\n\n| Drug | Dose |\n|---|---|\n"
        "| Aspirin | 81 mg |\n| Statin | 20 mg |\n\nAfter table."
    )
    flat, tables = flatten_markdown_tables(text)
    assert tables == 1
    assert "- Row 1: Drug=Aspirin; Dose=81 mg" in flat
    assert "|" not in flat
    assert lint_document("plain text, no headings") and not lint_document(
        "# Title\n\nShort section."
    )


def test_split_document_keeps_heading_path_and_stable_ids():
    """Chunks carry their heading path and get the same IDs on every split."""
    text = "# Policy\n\n## Scope\n\nApplies to all staff.\n\n## Exceptions\n\nNone."
    meta = DocumentMetadata(source_id="doc1", title="HR Policy", doc_type="policy")
    first = split_document(text, meta, chunk_size=200, chunk_overlap=0)
    second = split_document(text, meta, chunk_size=200, chunk_overlap=0)
    assert [c.section_path for c in first] == [
        ["Policy", "Scope"],
        ["Policy", "Exceptions"],
    ]
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert first[0].embedding_text().startswith("HR Policy > Policy > Scope")


def test_glossary_matches_whole_words_only():
    """Glossary notes apply only to whole-word term matches."""
    assert (
        glossary_notes("The ALB is up", {"ALB": "Application Load Balancer"})
        == "Definitions: ALB = Application Load Balancer"
    )
    assert glossary_notes("ALBUM", {"ALB": "x"}) == ""


def test_regex_detector_luhn_and_redaction():
    """The regex detector finds emails and Luhn-valid cards, classifies and redacts them."""
    detector = RegexDetector()
    text = "mail jane@example.com card 4111 1111 1111 1111 bogus 1234 5678 9012 3456"
    found = detector.detect(text)
    assert {d.category for d in found} == {"EMAIL", "PAYMENT_CARD"}
    assert (
        sum(d.category == "PAYMENT_CARD" for d in found) == 1
    )  # Luhn-invalid number ignored
    assert classify(found, Sensitivity.INTERNAL) is Sensitivity.PII
    assert "jane@example.com" not in redact(text, found)
    assert (
        classify([], Sensitivity.PHI) is Sensitivity.PHI
    )  # declared floor is never lowered


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (a:Entity)-[r*1..3]-(b) RETURN a.name, b.name LIMIT 10",
        # keywords inside literals are fine
        "MATCH (e:Entity {name: 'Set Theory; DELETE'}) RETURN e",
        "MATCH (e:Entity) CALL (e) { MATCH (e)--(n) RETURN count(n) AS c } RETURN e.name, c",
        "CALL db.labels() YIELD label RETURN label",
        "MATCH (n) RETURN n LIMIT 1;",
    ],
)
def test_cypher_guard_allows_reads(query):
    """Read-only Cypher, including keywords inside string literals, passes the guard."""
    assert validate_read_only_cypher(query)


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DETACH DELETE n",
        "match (n) set n.x = 1",
        "MERGE (n:Entity {key: 'x'})",
        "MATCH (n) RETURN n; MATCH (m) RETURN m",
        "CALL apoc.periodic.iterate('MATCH (n) RETURN n', 'DELETE n', {})",
        "LOAD CSV FROM 'file:///x' AS row RETURN row",
        "SHOW USERS",
        "MATCH (n) // harmless\nREMOVE n.name",
        "",
    ],
)
def test_cypher_guard_rejects_writes(query):
    """Writes, multiple statements, write procedures, file loads and admin commands fail."""
    with pytest.raises(UnsafeCypherError):
        validate_read_only_cypher(query)


def test_chunk_filter_enforces_clearance_and_scope():
    """ChunkFilter enforces the sensitivity ceiling, document types and effective-date range."""
    phi = make_chunk("d", 0, "phi text", Sensitivity.PHI)
    dated = make_chunk(
        "d", 1, "note", doc_type="discharge", effective_date=date(2025, 3, 1)
    )
    assert not ChunkFilter(max_sensitivity=Sensitivity.INTERNAL).allows(phi)
    assert ChunkFilter(max_sensitivity=Sensitivity.PHI).allows(phi)
    assert ChunkFilter(doc_types=["discharge"], effective_from=date(2025, 1, 1)).allows(
        dated
    )
    assert not ChunkFilter(effective_to=date(2024, 12, 31)).allows(dated)
    assert not ChunkFilter(effective_from=date(2025, 1, 1)).allows(
        make_chunk("d", 2, "undated")
    )


def test_vector_store_roundtrip_filter_and_stale_delete(settings, chunk_store):
    """The chunk store persists, fetches by ID in order, filters search and deletes stale chunks."""
    a = make_chunk("doc", 0, "alpha")
    b = make_chunk("doc", 1, "beta", Sensitivity.PHI)
    chunk_store.upsert([a, b], ["alpha", "beta"])
    reopened = LocalChunkStore(
        build_embeddings(settings), settings.vector_store_path
    )  # persisted
    assert [
        c.chunk_id
        for c in reopened.get_by_ids([b.chunk_id, a.chunk_id, "chk_0000000000000000"])
    ] == [b.chunk_id, a.chunk_id]
    hits = reopened.search("beta", 5, ChunkFilter(max_sensitivity=Sensitivity.INTERNAL))
    assert [c.chunk_id for c in hits] == [a.chunk_id]
    assert reopened.delete_source("doc", keep={a.chunk_id}) == 1
    assert [c.chunk_id for c in reopened.get_by_ids([a.chunk_id, b.chunk_id])] == [
        a.chunk_id
    ]


def test_plan_writes_dedupes_mentions_and_keeps_surface_forms():
    """Write planning uses resolved keys, keeps the original predicate and dedupes mentions."""
    acme = EntityRef(name="Acme", type="Organization", aliases=["AHI"])
    beta = EntityRef(name="Beta", type="Organization")
    t = Triple(head=acme, relation="acquired", tail=beta, evidence="Acme acquired Beta")
    head = ResolvedEntity(
        key="Organization:acme holdings",
        surface="Acme",
        method=Resolution.ALIAS,
        ref=acme,
    )
    tail = ResolvedEntity(key="Organization:beta", surface="Beta", ref=beta)
    row = GroundedTriple(
        triple=t,
        chunk_id="chk_" + "a" * 16,
        source_id="s",
        sensitivity=Sensitivity.PII,
        relation="ACQUIRED",
        original_relation="BOUGHT",
        head=head,
        tail=tail,
    )
    entities, mentions, relations = _plan_writes([row, row])
    assert list(relations) == ["ACQUIRED"] and len(relations["ACQUIRED"]) == 2
    assert (
        relations["ACQUIRED"][0]["head_key"] == "Organization:acme holdings"
    )  # resolved key, not extracted
    assert relations["ACQUIRED"][0]["predicate"] == "BOUGHT"
    assert {e["key"] for e in entities["Organization"]} == {
        "Organization:acme holdings",
        "Organization:beta",
    }
    assert {(m["surface"], m["method"]) for m in mentions} == {
        ("Acme", "alias"),
        ("AHI", "stated_alias"),
        ("Beta", "new"),
    }


def test_plan_writes_rejects_unsafe_relation_types():
    """Write planning refuses relation types that could inject Cypher."""
    ref = EntityRef(name="A", type="Organization")
    resolved = ResolvedEntity(key="Organization:a", surface="A", ref=ref)
    row = GroundedTriple(
        triple=Triple(head=ref, relation="x", tail=ref, evidence="e"),
        chunk_id="chk_" + "a" * 16,
        source_id="s",
        sensitivity=Sensitivity.PUBLIC,
        relation="X]->() DETACH DELETE (n",
        head=resolved,
        tail=resolved,
    )
    with pytest.raises(ValueError, match="unsafe relationship type"):
        _plan_writes([row])


def test_rank_case_follows_sensitivity_and_counts_unknown_levels_as_phi():
    """Each level maps to its rank; anything else, including null, ranks as phi."""
    for level in Sensitivity:
        assert f"WHEN '{level}' THEN {level.rank} " in _RANK
    assert _RANK.endswith(f"ELSE {Sensitivity.PHI.rank} END")


def _kernel(*versions):
    """dbms.components() rows for a kernel reporting these versions."""
    return [
        {"name": "Neo4j Kernel", "versions": list(versions)},
        {"name": "Cypher", "versions": ["5", "25"]},
    ]


@pytest.mark.parametrize(
    ("components", "expected"),
    [
        (_kernel("5.26.32"), _PROCEDURES),
        (_kernel("2025.12.0-aura"), _PROCEDURES),
        (_kernel("2026.08.1"), _PROCEDURES),
        (_kernel("2026.09.0"), _SEARCH_CLAUSE),
        (_kernel("2026.9"), _SEARCH_CLAUSE),
        (_kernel("2026.10.0-rc1"), _SEARCH_CLAUSE),
        (_kernel("2027.01.0"), _SEARCH_CLAUSE),
        # Unreadable versions fall back to the procedures every server has.
        (_kernel("5-aura"), _PROCEDURES),
        (_kernel("2026"), _PROCEDURES),
        (_kernel("dev"), _PROCEDURES),
        (_kernel(), _PROCEDURES),
        ([{"name": "Cypher", "versions": ["25"]}], _PROCEDURES),
    ],
)
def test_chunk_search_uses_the_search_clause_from_neo4j_2026_09(components, expected):
    """dbms.components() picks SEARCH on 2026.09+ and the queryNodes procedures otherwise."""
    assert index_queries(components) is expected


def test_unreadable_neo4j_version_is_logged_once(caplog):
    """The fallback names the version it could not read."""
    with caplog.at_level("WARNING", logger="nexo.storage.neo4j_chunk_store"):
        index_queries(_kernel("5-aura"))
    assert [r.getMessage() for r in caplog.records] == [
        "unreadable Neo4j version '5-aura': chunk search uses db.index.*.queryNodes"
    ]


class _DeniedSession:
    """A session whose reads fail as if dbms.components() were not granted."""

    calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute_read(self, *_args, **_kwargs):
        """Count the call and refuse it."""
        _DeniedSession.calls += 1
        # pylint: disable-next=protected-access  # no public constructor with a code
        raise Neo4jError._hydrate_neo4j(
            code="Neo.ClientError.Security.Forbidden", message="denied"
        )


class _DeniedGraph:  # pylint: disable=too-few-public-methods
    """Just enough GraphStore for Neo4jChunkStore._queries."""

    def session(self, *, write):
        """A session that refuses reads."""
        assert not write
        return _DeniedSession()


def test_denied_dbms_components_falls_back_to_procedures_once():
    """A server that refuses dbms.components() gets the procedures, decided once."""
    store = Neo4jChunkStore(
        _DeniedGraph(), DeterministicFakeEmbedding(size=4), dimensions=4
    )
    # pylint: disable=protected-access
    assert store._queries is _PROCEDURES
    assert store._queries is _PROCEDURES
    assert _DeniedSession.calls == 1
