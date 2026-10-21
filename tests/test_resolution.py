"""Ontology mapping and entity resolution, including the cases that must NOT merge."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexo.storage.ontology import FALLBACK_RELATION, Ontology
from nexo.storage.resolution import Candidate, EntityResolver
from nexo.storage.schemas import (
    EntityRef,
    EntityType,
    Resolution,
    entity_key,
    normalize_name,
)
from tests.conftest import RecordingChatModel

ONTOLOGY = Ontology.load(
    Path(__file__).resolve().parent.parent / "deploy" / "ontology.example.json"
)
ORG, PERSON, MED, COND = (
    EntityType.ORGANIZATION,
    EntityType.PERSON,
    EntityType.MEDICATION,
    EntityType.CONDITION,
)


# --- ontology -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "head", "tail", "expected"),
    [
        ("ACQUIRED", ORG, ORG, ("ACQUIRED", False, None)),
        ("BOUGHT", ORG, ORG, ("ACQUIRED", False, "BOUGHT")),
        ("ACQUIRED_BY", ORG, ORG, ("ACQUIRED", True, "ACQUIRED_BY")),
        ("COFOUNDER_OF", PERSON, ORG, ("FOUNDED", False, "COFOUNDER_OF")),
        (
            "FOUNDED",
            ORG,
            PERSON,
            ("FOUNDED", True, "FOUNDED"),
        ),  # extracted backwards: types flip it
        ("TREATED_WITH", COND, MED, ("TREATS", True, "TREATED_WITH")),
        (
            "TREATS",
            PERSON,
            COND,
            (FALLBACK_RELATION, False, "TREATS"),
        ),  # type constraint violated
        ("ADMIRES", ORG, ORG, (FALLBACK_RELATION, False, "ADMIRES")),
    ],
)
def test_ontology_mapping(predicate, head, tail, expected):
    """Synonyms, inverses and type-flipped predicates map to a canonical relation or fallback."""
    mapping = ONTOLOGY.map(predicate, head, tail)
    assert (mapping.relation, mapping.swap, mapping.original) == expected


def test_ontology_rejects_conflicting_aliases():
    """One synonym claimed by two relations fails validation."""
    with pytest.raises(ValidationError):
        Ontology.model_validate(
            {
                "relations": [
                    {"name": "A", "description": "a", "synonyms": ["X"]},
                    {"name": "B", "description": "b", "synonyms": ["X"]},
                ]
            }
        )


def test_example_ontology_is_valid_json_with_descriptions():
    """Every shipped example relation has a description and appears typed in the prompt."""
    raw = json.loads(
        (
            Path(__file__).resolve().parent.parent / "deploy" / "ontology.example.json"
        ).read_text()
    )
    assert all(r["description"] for r in raw["relations"])
    assert "ACQUIRED (Organization -> Organization)" in ONTOLOGY.prompt_block()


# --- normalization ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "type_", "expected"),
    [
        ("The Acme Holdings, Inc.", ORG, "acme holdings"),
        ("Acme Holdings Corp", ORG, "acme holdings"),
        ("Dr. Jane Roe", PERSON, "jane roe"),
        ("Metformin 500 mg", MED, "metformin 500 mg"),  # doses are kept
        ("Co", ORG, "co"),  # never strip a name down to nothing
        ("Inc. Magazine Inc.", ORG, "inc magazine"),
    ],
)
def test_normalize_name(name, type_, expected):
    """Articles, titles and legal suffixes are dropped without emptying the name."""
    assert normalize_name(name, type_) == expected


def test_legal_suffixes_are_only_stripped_for_organizations():
    """Non-organization names keep their corporate suffixes and titles."""
    assert entity_key("Acme Co", ORG) != entity_key("Acme Co", EntityType.PRODUCT)
    assert normalize_name("Dr. Pepper", EntityType.PRODUCT) == "dr pepper"


# --- resolution -------------------------------------------------------------------------


@dataclass
class FakeCatalog:
    """Candidate catalog that returns fixed candidates and records each lookup."""

    candidates: list[Candidate]
    calls: list[tuple] = field(default_factory=list)

    def find_candidates(self, entity_type, tokens, limit):
        """Return every candidate regardless of the requested type."""
        # Deliberately ignores entity_type: the resolver must not trust the catalog's filtering.
        self.calls.append((entity_type, tuple(tokens), limit))
        return list(self.candidates)


def ref(name, type_=ORG, aliases=(), description=""):
    """Build an EntityRef mention, defaulting to an organization."""
    return EntityRef(
        name=name, type=type_, aliases=list(aliases), description=description
    )


def test_existing_entity_exact_and_alias_matches_skip_the_model():
    """Exact-name and alias matches resolve deterministically without prompting the model."""
    catalog = FakeCatalog(
        [
            Candidate(
                key="Organization:acme holdings",
                name="Acme Holdings Inc.",
                aliases=["AHI"],
            )
        ]
    )
    llm = RecordingChatModel(replies=['{"decisions": []}'])
    resolved = EntityResolver(catalog=catalog, llm=llm).resolve(
        [ref("Acme Holdings"), ref("AHI")]
    )
    assert resolved["Organization:acme holdings"].method is Resolution.EXACT
    assert resolved["Organization:ahi"].key == "Organization:acme holdings"
    assert resolved["Organization:ahi"].method is Resolution.ALIAS
    assert llm.prompts == []


def test_model_merge_only_into_offered_candidates():
    """Model decisions naming a candidate that was not offered are ignored."""
    catalog = FakeCatalog(
        [
            Candidate(
                key="Person:edwin aldrin", name="Edwin Aldrin", description="astronaut"
            )
        ]
    )
    llm = RecordingChatModel(
        replies=[
            json.dumps(
                {
                    "decisions": [
                        {"mention": 0, "match": "Person:edwin aldrin"},
                        {
                            "mention": 1,
                            "match": "Person:invented key",
                        },  # not offered -> ignored
                    ]
                }
            )
        ]
    )
    resolved = EntityResolver(catalog=catalog, llm=llm).resolve(
        [
            ref("Buzz Aldrin", PERSON, description="Apollo 11 astronaut"),
            ref("Aldrin Smith", PERSON, description="accountant"),
        ]
    )
    assert resolved["Person:buzz aldrin"].key == "Person:edwin aldrin"
    assert resolved["Person:buzz aldrin"].method is Resolution.LLM
    assert (
        resolved["Person:aldrin smith"].key == "Person:aldrin smith"
    )  # stays separate
    assert "Edwin Aldrin" in llm.prompts[0][-1].content


def test_ambiguous_alias_goes_to_the_model_not_a_guess():
    """An alias shared by two entities is decided by the model, not picked arbitrarily."""
    catalog = FakeCatalog(
        [
            Candidate(key="Organization:apex bank", name="Apex Bank", aliases=["Apex"]),
            Candidate(
                key="Organization:apex mining", name="Apex Mining", aliases=["Apex"]
            ),
        ]
    )
    llm = RecordingChatModel(replies=['{"decisions": [{"mention": 0, "match": null}]}'])
    resolved = EntityResolver(catalog=catalog, llm=llm).resolve([ref("Apex")])
    assert resolved["Organization:apex"].key == "Organization:apex"
    assert len(llm.prompts) == 1


def test_types_never_merge_and_model_failure_keeps_mentions_separate():
    """Other-type candidates never match, and unparseable model output merges nothing."""
    catalog = FakeCatalog(
        [
            Candidate(key="Product:acme", name="Acme"),
            Candidate(
                key="Organization:acme widgets inc", name="Acme Widgets International"
            ),
        ]
    )
    llm = RecordingChatModel(replies=["not json"])
    resolved = EntityResolver(catalog=catalog, llm=llm).resolve(
        [ref("Acme"), ref("Acme Widgets")]
    )
    assert (
        resolved["Organization:acme"].key == "Organization:acme"
    )  # same-name Product never matches
    assert all(r.method is Resolution.NEW for r in resolved.values())
    assert catalog.calls[0][0] == ORG
    assert (
        len(llm.prompts) == 2
    )  # the model was consulted (and retried) but failed: nothing merged


def test_deterministic_mode_never_calls_the_model():
    """Deterministic mode leaves fuzzy matches unmerged instead of consulting the model."""
    catalog = FakeCatalog(
        [Candidate(key="Organization:beta labs", name="Beta Laboratories")]
    )
    llm = RecordingChatModel(
        replies=['{"decisions": [{"mention": 0, "match": "Organization:beta labs"}]}']
    )
    resolved = EntityResolver(catalog=catalog, llm=llm, mode="deterministic").resolve(
        [ref("Beta Labs Group")]
    )
    assert (
        resolved["Organization:beta labs group"].key == "Organization:beta labs group"
    )
    assert llm.prompts == []
