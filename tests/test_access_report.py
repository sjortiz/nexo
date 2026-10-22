"""`nexo access` and chunk sensitivity reasons."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from nexo.access_report import document_access, visibility
from nexo.agents.redaction import ChunkRedactor
from nexo.storage.schemas import ChunkRecord, DocumentMetadata, Sensitivity
from nexo.storage.sensitive_data import RegexDetector
from tests.conftest import RecordingChatModel
from tests.test_ingest import RecordingGraphWriter, _ingestor


def _chunk(text, level="internal", declared="internal", reasons=()):
    return ChunkRecord(
        chunk_id=ChunkRecord.make_id("doc", 0, text),
        source_id="doc",
        ordinal=0,
        text=text,
        sensitivity=level,
        declared_sensitivity=declared,
        sensitivity_reasons=list(reasons),
        ingested_at=datetime.now(UTC),
    )


def test_visibility_follows_the_redactor():
    """Within clearance: visible; above only for detected PII: masked; else withheld."""
    redactor = ChunkRedactor(RegexDetector())
    plain = _chunk("Acme acquired Beta.")
    phone = _chunk("Call 555-010-4477 for Acme.", level="pii", reasons=["PHONE"])
    declared = _chunk("Board notes.", level="pii", declared="pii")
    internal = Sensitivity.INTERNAL
    assert visibility(plain, internal, redactor) == "visible"
    assert visibility(phone, internal, redactor) == "masked"
    assert visibility(declared, internal, redactor) == "withheld"
    assert visibility(plain, Sensitivity.PUBLIC, redactor) == "withheld"


def test_document_access_counts_levels_reasons_and_views():
    """The per-document summary counts each clearance's view of every chunk."""
    redactor = ChunkRedactor(RegexDetector())
    records = [
        _chunk("Acme acquired Beta."),
        _chunk("Call 555-010-4477 for Acme.", level="pii", reasons=["PHONE"]),
    ]
    out = document_access(
        {"source_id": "doc", "title": "Memo", "declared": "internal"}, records, redactor
    )
    assert out["chunks"] == {"internal": 1, "pii": 1}
    assert out["reasons"] == {"PHONE": 1}
    assert out["sees"]["internal"] == {"visible": 1, "masked": 1}
    assert out["sees"]["public"] == {"withheld": 2}
    assert out["sees"]["pii"] == {"visible": 2}


def test_ingest_records_why_a_chunk_is_above_its_declared_level(settings, chunk_store):
    """Detected categories are stored with the chunk; redaction leaves none."""
    llm = RecordingChatModel(replies=[json.dumps({"triples": []})])
    writer = RecordingGraphWriter()
    _ingestor(settings, chunk_store, [], writer, llm=llm).ingest(
        "# Contacts\n\nReach the CFO at cfo@acme.example.",
        DocumentMetadata(source_id="contacts"),
    )
    [(_, chunks, _)] = writer.calls
    assert chunks[0].sensitivity is Sensitivity.PII
    assert chunks[0].sensitivity_reasons == ["EMAIL"]
