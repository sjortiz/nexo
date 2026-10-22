"""Canonical names (CANONICAL_NAMES_PATH) and their use in ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexo.storage.canonical_names import CanonicalNames
from nexo.storage.schemas import DocumentMetadata, EntityRef
from tests.conftest import RecordingChatModel
from tests.test_ingest import RecordingGraphWriter, _entity, _ingestor, _triple

EXAMPLE = (
    Path(__file__).resolve().parent.parent / "deploy" / "canonical-names.example.yaml"
)
NAMES = {
    "entities": [
        {"name": "Acme Holdings", "type": "Organization", "aliases": ["AHI", "Acme"]},
    ]
}


def test_example_file_loads():
    """The shipped example is valid."""
    assert CanonicalNames.load(EXAMPLE).entities


def test_apply_rewrites_name_and_type_and_keeps_the_wording():
    """Any type and casing of an alias maps to the canonical name and type."""
    names = CanonicalNames.model_validate(NAMES)
    out = names.apply(EntityRef(name="ahi", type="Concept"))
    assert (out.name, out.type, out.aliases) == (
        "Acme Holdings",
        "Organization",
        ["ahi"],
    )
    other = EntityRef(name="Beta Labs", type="Organization")
    assert names.apply(other) is other


def test_a_name_has_one_owner():
    """The same alias under two canonical entities is rejected."""
    bad = {
        "entities": [
            *NAMES["entities"],
            {"name": "Acme Corp", "type": "Organization", "aliases": ["acme"]},
        ]
    }
    with pytest.raises(ValidationError, match="belongs to both"):
        CanonicalNames.model_validate(bad)


def test_record_and_save_round_trip(tmp_path):
    """Merges add entries or extend an existing one; the file round-trips."""
    path = tmp_path / "local" / "canonical.yaml"
    names = CanonicalNames.load(path)  # missing file: empty
    names.record("Acme Holdings", "Organization", ["Acme", "AHI"])
    names.record(
        "Acme", "Organization", ["Acme Holdings Inc"]
    )  # alias of the same entity
    names.save(path)
    again = CanonicalNames.load(path)
    [entity] = again.entities
    assert entity.name == "Acme Holdings"
    assert entity.aliases == ["Acme", "AHI", "Acme Holdings Inc"]


def test_ingest_applies_canonical_names(settings, chunk_store):
    """Mentions are rewritten before resolution, so they land on one key."""
    reply = {
        "triples": [
            _triple(
                "Acme Holdings acquired Beta Labs",
                head=_entity("AHI", "Concept"),
            )
        ]
    }
    llm = RecordingChatModel(replies=[json.dumps(reply)])
    writer = RecordingGraphWriter()
    report = _ingestor(
        settings,
        chunk_store,
        [],
        writer,
        llm=llm,
        canonical_names=CanonicalNames.model_validate(NAMES),
    ).ingest(
        "# Deals\n\nAcme Holdings acquired Beta Labs in 2024.",
        DocumentMetadata(source_id="deals"),
    )
    [(_, _, triples)] = writer.calls
    assert triples[0].head.key == "Organization:acme holdings"
    assert report.converted == {"canonical_name": 1}
