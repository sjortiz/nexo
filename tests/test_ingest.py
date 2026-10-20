"""Document ingestion: extraction, evidence grounding, redaction, ontology and resolution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nexo.storage.ingest import ExtractionAids, IngestionTargets, KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology
from nexo.storage.resolution import EntityResolver
from nexo.storage.schemas import DocumentMetadata, Resolution, Sensitivity, SourceSwap
from nexo.storage.sensitive_data import RegexDetector
from nexo.storage.vector_store import ChunkFilter
from tests.conftest import RecordingChatModel

DOC = "# Deals\n\nAcme Holdings acquired Beta Labs in 2024. Contact: cfo@acme.example."
ONTOLOGY = Ontology.load(
    Path(__file__).resolve().parent.parent / "deploy" / "ontology.example.json"
)


@dataclass
class RecordingGraphWriter:
    """Graph writer that records each replace_source call instead of writing to Neo4j."""

    calls: list[tuple] = field(default_factory=list)

    def replace_source(self, document, chunks, triples):
        """Record the document, chunks and triples; report every triple as written."""
        self.calls.append((document, list(chunks), list(triples)))
        return SourceSwap(triples_written=len(triples), chunks_removed=0)


def _entity(name, type_="Organization", aliases=()):
    return {"name": name, "type": type_, "aliases": list(aliases)}


def _triple(evidence: str, relation: str = "acquired", head=None, tail=None) -> dict:
    return {
        "head": head or _entity("Acme Holdings"),
        "relation": relation,
        "tail": tail or _entity("Beta Labs"),
        "evidence": evidence,
        "valid_at": "2024-01-01",
    }


def _ingestor(settings, chunk_store, replies, writer, **kwargs):
    llm = kwargs.pop("llm", None) or GenericFakeChatModel(
        messages=iter([AIMessage(content=r) for r in replies])
    )
    return KnowledgeGraphIngestor(
        llm=llm,
        detector=RegexDetector(),
        settings=settings,
        targets=IngestionTargets(chunk_store=chunk_store, graph_writer=writer),
        aids=ExtractionAids(**kwargs),
    )


def test_ingest_validates_grounds_and_links_chunks(settings, chunk_store):
    """Schema errors retry, ungrounded triples drop, and written chunks resolve in the store."""
    writer = RecordingGraphWriter()
    replies = [
        json.dumps(
            {"triples": [], "confidence": 1}
        ),  # schema violation -> fed back, retried
        json.dumps(
            {
                "triples": [
                    _triple(
                        "Acme Holdings acquired  Beta Labs"
                    ),  # whitespace-tolerant match
                    _triple("Acme bought Beta for $1B", "valued"),
                ]
            }
        ),  # not in text -> dropped
    ]
    report = _ingestor(settings, chunk_store, replies, writer).ingest(
        DOC, DocumentMetadata(source_id="deals", title="Deals memo")
    )

    assert report.failed_chunk_ids == []
    assert report.triples_extracted == 2 and report.triples_written == 1
    assert report.dropped == {"evidence_not_in_chunk": 1}
    assert report.sensitivity == {"pii": 1}  # email detected, floor was internal
    [(document, chunks, triples)] = writer.calls
    assert document.source_id == "deals" and [c.chunk_id for c in chunks] == [
        triples[0].chunk_id
    ]
    # The graph/vector contract: every chunk_id written to Neo4j resolves in the vector store.
    [stored] = chunk_store.get_by_ids([triples[0].chunk_id])
    assert (
        stored.sensitivity is Sensitivity.PII
        and "Acme Holdings acquired" in stored.text
        and stored.declared_sensitivity
        is Sensitivity.INTERNAL  # the floor, not the detected level
    )


def test_ingest_reports_failed_chunks_without_aborting(settings, chunk_store):
    """A chunk whose extraction keeps failing is reported, kept, and still written lexically."""
    writer = RecordingGraphWriter()
    report = _ingestor(settings, chunk_store, ["not json"] * 3, writer).ingest(
        DOC, DocumentMetadata(source_id="deals")
    )
    assert len(report.failed_chunk_ids) == 1 and report.triples_written == 0
    assert chunk_store.get_by_ids(
        report.failed_chunk_ids
    )  # chunk kept for re-extraction
    [(_, chunks, triples)] = writer.calls
    assert len(chunks) == 1 and triples == []  # lexical graph still written


def test_reingest_replaces_stale_chunks(settings, chunk_store):
    """Re-ingesting a changed document deletes the chunk it no longer contains."""
    writer = RecordingGraphWriter()
    empty = json.dumps({"triples": []})
    first = _ingestor(settings, chunk_store, [empty], writer).ingest(
        DOC, DocumentMetadata(source_id="deals")
    )
    second = _ingestor(settings, chunk_store, [empty], writer).ingest(
        DOC.replace("2024", "2025"), DocumentMetadata(source_id="deals")
    )
    assert second.stale_chunks_deleted == 1
    assert first.chunk_count == second.chunk_count == 1


def test_redact_policy_removes_detected_values(settings, chunk_store):
    """The redact policy stores chunks with detected values replaced by placeholders."""
    settings.sensitive_data_policy = "redact"
    writer = RecordingGraphWriter()
    _ingestor(settings, chunk_store, [json.dumps({"triples": []})], writer).ingest(
        DOC, DocumentMetadata(source_id="deals")
    )
    [chunk] = chunk_store.search("Acme", 1, ChunkFilter())
    assert "cfo@acme.example" not in chunk.text and "[EMAIL]" in chunk.text


def test_previous_chunk_is_context_but_never_evidence(settings, chunk_store):
    """The previous chunk appears as prompt context but cannot ground the next chunk's triples."""
    settings.chunk_size, settings.extraction_context_chars = 60, 100
    text = (
        "# Deals\n\nAcme Holdings Inc. is a holding company based in Ohio.\n\n"
        "It acquired Beta Labs last spring after long talks."
    )
    llm = RecordingChatModel(
        replies=[
            json.dumps(
                {
                    "triples": [
                        _triple(
                            "Acme Holdings Inc. is a holding company"
                        ),  # quote from the first chunk only
                        _triple(
                            "It acquired Beta Labs"
                        ),  # quote from the second chunk only
                    ]
                }
            )
        ]
    )
    writer = RecordingGraphWriter()
    report = _ingestor(settings, chunk_store, [], writer, llm=llm).ingest(
        text, DocumentMetadata(source_id="d")
    )

    assert report.chunk_count == 2
    first_prompt, second_prompt = (p[-1].content for p in llm.prompts)
    assert "Preceding text" not in first_prompt
    assert "Preceding text (context only):\n...Acme Holdings Inc." in second_prompt
    assert second_prompt.index("Preceding text") < second_prompt.index(
        "Passage:\nIt acquired Beta Labs"
    )
    [(_, chunks, triples)] = writer.calls
    # Each chunk keeps only the triple whose quote it contains, even though the
    # second chunk's prompt showed the first chunk's text as context.
    assert sorted(t.chunk_id for t in triples) == sorted(c.chunk_id for c in chunks)
    assert report.dropped["evidence_not_in_chunk"] == 2


def test_ontology_maps_synonyms_inverses_and_unknown_predicates(settings, chunk_store):
    """The ontology swaps inverses, maps synonyms and sends unknown predicates to RELATED_TO."""
    text = (
        "# Deals\n\nBeta Labs was acquired by Acme Holdings. Acme Holdings bought Beta Labs. "
        "Acme Holdings admires Beta Labs."
    )
    replies = [
        json.dumps(
            {
                "triples": [
                    _triple(
                        "Beta Labs was acquired by Acme Holdings",
                        "acquired by",
                        head=_entity("Beta Labs"),
                        tail=_entity("Acme Holdings"),
                    ),  # inverse -> swapped
                    _triple("Acme Holdings bought Beta Labs", "bought"),  # synonym
                    _triple(
                        "Acme Holdings admires Beta Labs", "admires"
                    ),  # not in ontology
                ]
            }
        )
    ]
    writer = RecordingGraphWriter()
    report = _ingestor(
        settings, chunk_store, replies, writer, ontology=ONTOLOGY
    ).ingest(text, DocumentMetadata(source_id="d"))
    [(_, _, triples)] = writer.calls
    written = [
        (t.head.key, t.relation, t.tail.key, t.original_relation) for t in triples
    ]
    assert written == [
        (
            "Organization:acme holdings",
            "ACQUIRED",
            "Organization:beta labs",
            "ACQUIRED_BY",
        ),
        ("Organization:acme holdings", "ACQUIRED", "Organization:beta labs", "BOUGHT"),
        (
            "Organization:acme holdings",
            "RELATED_TO",
            "Organization:beta labs",
            "ADMIRES",
        ),
    ]
    assert report.relations == {"remapped": 2, "fallback": 1}


def test_resolution_merges_names_within_a_document(settings, chunk_store):
    """Deterministic resolution merges aliases into one entity and drops resulting self-loops."""
    text = "# Deals\n\nAcme Holdings Inc. (Acme) acquired Beta Labs. Acme also acquired Gamma."
    replies = [
        json.dumps(
            {
                "triples": [
                    _triple(
                        "Acme Holdings Inc. (Acme) acquired Beta Labs",
                        head=_entity("Acme Holdings Inc.", aliases=["Acme"]),
                    ),
                    _triple(
                        "Acme also acquired Gamma",
                        head=_entity("Acme"),
                        tail=_entity("Gamma"),
                    ),
                    _triple(
                        "Acme Holdings Inc. (Acme)",
                        "also known as",  # self-reference once merged
                        head=_entity("Acme Holdings Inc."),
                        tail=_entity("Acme"),
                    ),
                ]
            }
        )
    ]
    writer = RecordingGraphWriter()
    report = _ingestor(
        settings,
        chunk_store,
        replies,
        writer,
        resolver=EntityResolver(catalog=None, llm=None, mode="deterministic"),
    ).ingest(text, DocumentMetadata(source_id="d"))
    [(_, _, triples)] = writer.calls
    heads = {(t.head.key, t.head.surface, t.head.method) for t in triples}
    assert heads == {
        ("Organization:acme holdings", "Acme Holdings Inc.", Resolution.NEW),
        ("Organization:acme holdings", "Acme", Resolution.ALIAS),
    }
    assert report.dropped == {"self_loop_after_resolution": 1}


def test_resolution_off_keeps_names_separate(settings, chunk_store):
    """With resolution off, an alias stays a separate entity."""
    replies = [
        json.dumps(
            {
                "triples": [
                    _triple(
                        "Acme Holdings acquired Beta Labs",
                        head=_entity("Acme Holdings", aliases=["Acme"]),
                    ),
                    _triple(
                        "Acme Holdings acquired Beta Labs",
                        head=_entity("Acme"),
                        tail=_entity("Beta Labs"),
                    ),
                ]
            }
        )
    ]
    writer = RecordingGraphWriter()
    _ingestor(
        settings,
        chunk_store,
        replies,
        writer,
        resolver=EntityResolver(catalog=None, llm=None, mode="off"),
    ).ingest(DOC, DocumentMetadata(source_id="d"))
    [(_, _, triples)] = writer.calls
    assert {t.head.key for t in triples} == {
        "Organization:acme holdings",
        "Organization:acme",
    }
