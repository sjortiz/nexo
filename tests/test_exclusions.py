"""Ingest exclusions (INGEST_EXCLUSIONS_PATH) and amount entities (AMOUNT_ENTITIES)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexo.storage.amounts import attribute_name, is_amount
from nexo.storage.entity_types import EntityTypes
from nexo.storage.exclusions import IngestExclusions, stage_texts, strip_markers
from nexo.storage.graph_store import _plan_writes
from nexo.storage.schemas import (
    Attribute,
    DocumentMetadata,
    EntityRef,
    GroundedAttribute,
    ResolvedEntity,
)
from tests.conftest import RecordingChatModel
from tests.test_ingest import RecordingGraphWriter, _entity, _ingestor, _triple

EXAMPLE = (
    Path(__file__).resolve().parent.parent / "deploy" / "ingest-exclusions.example.yaml"
)
IMAGE_RULE = {
    "name": "images",
    "start": "^<!-- image:",
    "while": "^>",
    "stages": ["extraction"],
}
DOC = (
    "# Clinics\n\nGamma Clinics is a clinic network in Ohio.\n\n"
    "<!-- image:img01 -->\n> **Image:** Vita Plus product tile, $35.\n\n"
    "Gamma Clinics is rated 4.8 stars."
)


def _rules(*rules) -> IngestExclusions:
    return IngestExclusions.model_validate({"exclude": list(rules)})


# --- rules --------------------------------------------------------------------


def test_example_file_loads():
    """The shipped example is a valid exclusions file."""
    rules = IngestExclusions.load(EXAMPLE)
    assert [r.name for r in rules.exclude][0] == "image-descriptions"


@pytest.mark.parametrize(
    "rule, message",
    [
        ({"name": "x", "stages": ["search"]}, "exactly one of"),
        (
            {"name": "x", "lines": "a", "entity": "b", "stages": ["search"]},
            "exactly one of",
        ),
        ({"name": "x", "lines": "a", "while": "b", "stages": ["search"]}, "need start"),
        (
            {"name": "x", "start": "a", "while": "b", "end": "c", "stages": ["search"]},
            "not both",
        ),
        ({"name": "x", "entity": "a", "stages": ["search"]}, "extraction only"),
        ({"name": "x", "lines": "(", "stages": ["search"]}, "invalid regex"),
        ({"name": "x", "lines": "a", "stages": ["index"]}, "stages"),
    ],
)
def test_invalid_rules_are_rejected(rule, message):
    """A rule needs one selector, valid regexes and known stages."""
    with pytest.raises(ValidationError, match=message):
        _rules(rule)


def test_rule_names_must_be_unique():
    """Two rules with one name would make the report ambiguous."""
    rule = {"name": "x", "lines": "a", "stages": ["search"]}
    with pytest.raises(ValidationError, match="unique"):
        _rules(rule, rule)


def test_block_selectors():
    """`while` continues while lines match; `end` runs through the closing line."""
    lines = ["a", "<!-- image:1 -->", "> one", "> two", "b", "SKIP", "x", "/SKIP", "c"]
    images = _rules(IMAGE_RULE).exclude[0]
    skip = _rules(
        {"name": "s", "start": "^SKIP$", "end": "^/SKIP$", "stages": ["search"]}
    ).exclude[0]
    assert images.regions(lines) == [(1, 4)]
    assert skip.regions(lines) == [(5, 8)]


def test_document_stage_removes_text_and_doc_types_scope_rules():
    """`document` regions are gone before chunking; doc_types limits where a rule applies."""
    rules = _rules(
        {"name": "footer", "lines": "^Confidential", "stages": ["document"]},
        {**IMAGE_RULE, "doc_types": ["memo"]},
    )
    marked = rules.mark(
        "Body\nConfidential - do not share\n<!-- image:1 -->\n> pic", "web"
    )
    assert marked.text == "Body\n<!-- image:1 -->\n> pic"
    assert dict(marked.matched) == {"footer": 1}


def test_a_region_split_across_chunks_is_cut_from_each():
    """Chunks that start inside a region, or lie wholly inside one, lose that text."""
    marked = (
        _rules(IMAGE_RULE)
        .mark("a\n<!-- image:1 -->\n> one\n> two\n> three\nb", "x")
        .text
    )
    lines = marked.split("\n")
    # Split by hand: [a, open, asset, one] [two] [three, close, b]
    chunks = ["\n".join(lines[:4]), lines[4], "\n".join(lines[5:])]
    assert stage_texts(chunks, "extraction") == ["a", "", "b"]
    assert stage_texts(chunks, "search") == [
        "a\n<!-- image:1 -->\n> one",
        "> two",
        "> three\nb",
    ]
    assert strip_markers(marked) == "a\n<!-- image:1 -->\n> one\n> two\n> three\nb"


# --- amounts --------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "4.8 stars",
        "77 reviews",
        "1.6k reviews",
        "$35",
        "$22.50",
        "28%",
        "60 days",
        "~5 miles",
        "$10 - $20",
        "$3M",
        "2024-03-01",
        "Q3 2024",
        "42",
    ],
)
def test_amounts(name):
    """Names that are only a quantity, price, percentage or date are amounts."""
    assert is_amount(name)


@pytest.mark.parametrize(
    "name",
    [
        "$5 Lunch Special",
        "30% off First Time Customers",
        "3M",
        "3 Musketeers",
        "7-Eleven",
        "Top 10 Suppliers",
        "Area 51",
        "500k",
        "Acme Holdings",
    ],
)
def test_names_with_numbers_are_not_amounts(name):
    """A number inside a name does not make it an amount."""
    assert not is_amount(name)


def test_attribute_name_from_relation():
    """HAS_/IS_ prefixes go; the rest becomes a lower-case attribute name."""
    assert attribute_name("HAS_STAR_RATING") == "star rating"
    assert attribute_name("PRICED_AT") == "priced at"


# --- ingestion ----------------------------------------------------------------------


def _shop_ingest(settings, chunk_store, reply, **aids):
    llm = RecordingChatModel(replies=[json.dumps(reply)])
    writer = RecordingGraphWriter()
    upserts = []
    original = chunk_store.upsert

    def spy(chunks, texts):
        upserts.append(list(texts))
        return original(chunks, texts)

    chunk_store.upsert = spy
    report = _ingestor(settings, chunk_store, [], writer, llm=llm, **aids).ingest(
        DOC, DocumentMetadata(source_id="shops", doc_type="memo")
    )
    return report, writer, llm, upserts


def test_extraction_exclusion_keeps_text_searchable_but_out_of_the_prompt(
    settings, chunk_store
):
    """An extraction-only region is stored and indexed, never sent to the model."""
    reply = {"triples": [_triple("$35", "costs", tail=_entity("Vita Plus", "Product"))]}
    report, writer, llm, upserts = _shop_ingest(
        settings, chunk_store, reply, exclusions=_rules(IMAGE_RULE)
    )
    prompt = "\n".join(m.content for m in llm.prompts[0])
    assert "Vita Plus" not in prompt and "rated 4.8 stars" in prompt
    assert any("Vita Plus" in t for t in upserts[0])  # still searchable
    [(_, chunks, _)] = writer.calls
    assert "Vita Plus" in chunks[0].text  # still stored and citable
    assert report.excluded == {"images": 1}
    assert report.dropped == {
        "evidence_not_in_chunk": 1
    }  # quote from the excluded text


def test_search_exclusion_leaves_the_index_only(settings, chunk_store):
    """A search-only region is extracted and stored but not embedded."""
    rule = {**IMAGE_RULE, "stages": ["search"]}
    _, writer, llm, upserts = _shop_ingest(
        settings, chunk_store, {"triples": []}, exclusions=_rules(rule)
    )
    assert "Vita Plus" in "\n".join(m.content for m in llm.prompts[0])
    assert not any("Vita Plus" in t for t in upserts[0])
    assert "Vita Plus" in writer.calls[0][1][0].text


def test_entity_rule_drops_triples(settings, chunk_store):
    """A triple naming an excluded entity is dropped and counted by rule."""
    rule = {"name": "places", "entity": "^Ohio$", "stages": ["extraction"]}
    reply = {
        "triples": [
            _triple(
                "Gamma Clinics is a clinic network in Ohio",
                "located_in",
                head=_entity("Gamma Clinics"),
                tail=_entity("Ohio", "Location"),
            )
        ]
    }
    report, _, _, _ = _shop_ingest(
        settings, chunk_store, reply, exclusions=_rules(rule)
    )
    assert report.dropped == {"excluded_entity:places": 1}


AMOUNT_REPLY = {
    "triples": [
        _triple(
            "Gamma Clinics is rated 4.8 stars",
            "has_star_rating",
            head=_entity("Gamma Clinics"),
            tail=_entity("4.8 stars", "Concept"),
        )
    ],
    "attributes": [
        {
            "entity": _entity("Gamma Clinics"),
            "name": "rating",
            "value": "4.8 stars",
            "evidence": "rated 4.8 stars",
        },
        {
            "entity": _entity("Gamma Clinics"),
            "name": "price",
            "value": "$9",
            "evidence": "costs $9",  # not in the text
        },
    ],
}


def test_amount_entities_convert_to_attributes(settings, chunk_store):
    """By default an amount tail becomes an attribute of the head; attributes are grounded."""
    report, writer, _, _ = _shop_ingest(settings, chunk_store, AMOUNT_REPLY)
    assert report.converted == {"amount_entity": 1}
    assert report.dropped == {"attribute_evidence_not_in_chunk": 1}
    assert report.triples_written == 0 and report.attributes_written == 2
    [attributes] = writer.attributes
    assert {(a.attribute.name, a.attribute.value) for a in attributes} == {
        ("star rating", "4.8 stars"),
        ("rating", "4.8 stars"),
    }
    assert {a.entity.key for a in attributes} == {"Organization:gamma clinics"}


@pytest.mark.parametrize(
    "mode, dropped, written",
    [("drop", {"amount_entity": 1}, 0), ("keep", {}, 1)],
)
def test_amount_entities_drop_and_keep(settings, chunk_store, mode, dropped, written):
    """`drop` discards the triple; `keep` writes it as before."""
    settings.amount_entities = mode
    reply = {"triples": AMOUNT_REPLY["triples"]}
    report, _, _, _ = _shop_ingest(settings, chunk_store, reply)
    assert report.dropped == dropped and report.triples_written == written


def test_attributes_are_planned_on_the_mention():
    """Attributes ride on MENTIONED_IN as JSON, so the mention's withdrawal removes them."""
    ref = EntityRef(name="Gamma Clinics", type="Organization")
    placed = GroundedAttribute(
        attribute=Attribute(entity=ref, name="rating", value="4.8 stars", evidence="e"),
        chunk_id="chk_" + "a" * 16,
        source_id="shops",
        entity=ResolvedEntity(key=ref.key, surface=ref.name, ref=ref),
    )
    entities, mentions, relations = _plan_writes([], [placed])
    assert relations == {} and [e["key"] for e in entities["Organization"]] == [ref.key]
    [mention] = mentions
    assert json.loads(mention["attributes_json"]) == [
        {"evidence": "e", "name": "rating", "value": "4.8 stars"}
    ]


# --- entity types (ENTITY_TYPES_PATH) -----------------------------------------------


def test_entity_types_example_loads_and_limits_the_schema():
    """The example file drops clinical built-ins and adds custom types to the schema."""
    types = EntityTypes.load(EXAMPLE.parent / "entity-types.example.yaml")
    assert "Contract" in types.names and "Medication" not in types.names
    schema = types.extraction_schema
    ok = {"name": "MSA", "type": "Contract"}
    schema.model_validate(
        {"triples": [{"head": ok, "relation": "R", "tail": ok, "evidence": "e"}]}
    )
    with pytest.raises(ValidationError):
        schema.model_validate(
            {
                "attributes": [
                    {
                        "entity": {"name": "X", "type": "Medication"},
                        "name": "n",
                        "value": "1",
                        "evidence": "e",
                    }
                ]
            }
        )
    assert "Contract: a signed agreement" in types.prompt_block()
    assert "Person: a named individual" in types.prompt_block()


@pytest.mark.parametrize(
    "config, message",
    [
        (
            {"builtin": ["Person"], "custom": [{"name": "Person", "description": "x"}]},
            "unique",
        ),
        ({"builtin": [], "custom": []}, "at least one"),
        ({"custom": [{"name": "bad name", "description": "x"}]}, "pattern"),
        ({"builtin": ["Spaceship"]}, "builtin"),
    ],
)
def test_invalid_entity_types_are_rejected(config, message):
    """Type names are unique, safe labels; built-ins must exist."""
    with pytest.raises(ValidationError, match=message):
        EntityTypes.model_validate(config)


def test_custom_type_reaches_the_prompt_and_the_graph(settings, chunk_store):
    """A custom type is offered to the model and written; a disabled one is dropped."""
    types = EntityTypes(
        builtin=["Organization"],
        custom=[
            {"name": "Audience", "description": "a group of patients or customers"}
        ],
    )
    reply = {
        "triples": [
            _triple(
                "Gamma Clinics is a clinic network in Ohio",
                "serves",
                head=_entity("Gamma Clinics"),
                tail=_entity("clinic patients", "Audience"),
            )
        ]
    }
    report, writer, llm, _ = _shop_ingest(
        settings, chunk_store, reply, entity_types=types
    )
    assert "Audience: a group of patients or customers" in llm.prompts[0][0].content
    assert report.triples_written == 1
    assert writer.calls[0][2][0].tail.key == "Audience:clinic patients"
