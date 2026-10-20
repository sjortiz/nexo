"""Pydantic v2 schemas for documents, chunks and extracted knowledge.

Extraction follows the knowledge-graph guidance in the Anthropic Claude
Cookbook: a closed set of entity types, specific directional predicates, only
facts stated in the text, canonical names, and a verbatim evidence quote per
triple so every edge can be checked against its source chunk. The AWS
healthcare RAG guidance adds certainty and temporal qualifiers on edges and
terminology codes on nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

import hashlib
import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

SCHEMA_VERSION = "1"

CHUNK_ID_PATTERN = r"^chk_[0-9a-f]{16}$"
CHUNK_ID_RE = re.compile(CHUNK_ID_PATTERN)
RELATION_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

ShortText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class Sensitivity(StrEnum):
    """Ordered data classification; a caller sees chunks at or below its clearance."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    PHI = "phi"

    @property
    def rank(self) -> int:
        """Position in the ordering, 0 (public) to 3 (phi)."""
        return list(Sensitivity).index(self)


class EntityType(StrEnum):
    """Entity types extraction may assign."""

    # Closed set: values double as Neo4j labels, so they must stay literals.
    PERSON = "Person"
    ORGANIZATION = "Organization"
    LOCATION = "Location"
    PRODUCT = "Product"
    EVENT = "Event"
    CONCEPT = "Concept"
    CONDITION = "Condition"
    MEDICATION = "Medication"
    PROCEDURE = "Procedure"


class CodeSystem(StrEnum):
    """Terminology systems a proposed code may come from."""

    ICD10CM = "ICD10CM"
    RXNORM = "RXNORM"
    SNOMEDCT = "SNOMEDCT"
    MESH = "MESH"
    UMLS = "UMLS"


class Certainty(StrEnum):
    """Whether the text asserts, hedges or rules out a fact."""

    ASSERTED = "asserted"
    POSSIBLE = "possible"
    NEGATED = "negated"


# --- documents and chunks ---------------------------------------------------


class DocumentMetadata(BaseModel):
    """Document-level metadata copied onto every chunk (filterable at retrieval)."""

    model_config = ConfigDict(extra="forbid")

    source_id: ShortText = Field(
        description="Stable document id; re-ingesting the same id replaces it"
    )
    title: str = ""
    doc_type: str = "general"
    source_uri: str | None = None
    effective_date: date | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL


class ChunkRecord(BaseModel):
    """One stored chunk: exact text plus the document metadata used for filtering."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: Annotated[str, StringConstraints(pattern=CHUNK_ID_PATTERN)]
    source_id: str
    ordinal: int = Field(ge=0)
    text: str = Field(
        description="Exact source text; the evidence that extraction is checked against"
    )
    section_path: list[str] = Field(default_factory=list)
    title: str = ""
    doc_type: str = "general"
    source_uri: str | None = None
    effective_date: date | None = None
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    ingested_at: datetime
    schema_version: str = SCHEMA_VERSION

    @staticmethod
    def make_id(source_id: str, ordinal: int, text: str) -> str:
        """Deterministic id: re-ingesting identical content is an idempotent upsert."""
        digest = hashlib.sha256(
            f"{source_id}\x1f{ordinal}\x1f{text}".encode()
        ).hexdigest()
        return f"chk_{digest[:16]}"

    def embedding_text(self, glossary_notes: str = "") -> str:
        """Text that gets embedded: contextual header + chunk + glossary.

        Prepending the document title and heading path makes a chunk
        self-contained, which is what the AWS RAG writing guidance asks of
        source sections.
        """
        header = " > ".join([p for p in [self.title, *self.section_path] if p])
        parts = [header, self.text, glossary_notes]
        return "\n\n".join(p for p in parts if p)

    def filter_metadata(self) -> dict[str, Any]:
        """Metadata fields that retrieval filters on, as JSON-ready values."""
        return {
            "source_id": self.source_id,
            "doc_type": self.doc_type,
            "effective_date": (
                self.effective_date.isoformat() if self.effective_date else None
            ),
            "sensitivity": self.sensitivity.value,
        }


# --- extraction ------------------------------------------------------------------


def normalize_relation(value: str) -> str:
    """'works for' / 'worksFor' / 'WORKS-FOR' -> 'WORKS_FOR'."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value.strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", spaced).strip("_").upper()
    if normalized and normalized[0].isdigit():
        normalized = f"R_{normalized}"
    return normalized


_LEGAL_SUFFIXES = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "llc",
    "ltd",
    "limited",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "srl",
    "sas",
    "nv",
    "bv",
    "lp",
    "llp",
}
_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "prof",
    "sir",
    "dame",
    "sr",
    "sra",
    "srta",
    "lic",
    "ing",
}


def normalize_name(name: str, entity_type: EntityType) -> str:
    """Surface form -> comparison form: 'The Acme Holdings, Inc.' -> 'acme holdings'.

    Only type-safe rules: legal suffixes for organizations, honorifics for
    people. Medication/condition names keep every token (dose, variant).
    """
    tokens = re.sub(r"[^\w\s&-]", " ", name.casefold()).split()
    if tokens and tokens[0] == "the" and len(tokens) > 1:
        tokens = tokens[1:]
    if entity_type is EntityType.ORGANIZATION:
        while len(tokens) > 1 and tokens[-1] in _LEGAL_SUFFIXES:
            tokens = tokens[:-1]
    if entity_type is EntityType.PERSON:
        while len(tokens) > 1 and tokens[0] in _HONORIFICS:
            tokens = tokens[1:]
    return " ".join(tokens)


def entity_key(name: str, entity_type: EntityType) -> str:
    """Exact-match key: type + normalized name. Fuzzier merges happen in resolution."""
    return f"{entity_type.value}:{normalize_name(name, entity_type)}"


class EntityRef(BaseModel):
    """An entity as named and described in the text."""

    model_config = ConfigDict(extra="forbid")

    name: ShortText = Field(
        description="Canonical full name as written in the text, pronouns resolved"
    )
    type: EntityType
    description: str = Field(
        default="", max_length=300, description="Short description grounded in the text"
    )
    aliases: list[ShortText] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Other names the passage uses for this same entity "
            "(acronyms, short or former names)"
        ),
    )
    proposed_codes: dict[CodeSystem, str] = Field(
        default_factory=dict,
        description=(
            "Terminology codes if the text states them. "
            "Unverified until checked by a terminology service."
        ),
    )

    @property
    def key(self) -> str:
        """Exact-match entity key from type and normalized name."""
        return entity_key(self.name, self.type)


class Triple(BaseModel):
    """One entity-relation-entity fact extracted from a single chunk."""

    model_config = ConfigDict(extra="forbid")

    head: EntityRef
    relation: str = Field(
        description="Directional predicate in UPPER_SNAKE_CASE, e.g. ACQUIRED, PRESCRIBED"
    )
    tail: EntityRef
    evidence: str = Field(
        min_length=1,
        max_length=400,
        description="Verbatim quote from the text supporting the fact",
    )
    certainty: Certainty = Certainty.ASSERTED
    valid_at: date | None = Field(
        default=None, description="Date the fact holds, only if stated"
    )

    @field_validator("relation", mode="before")
    @classmethod
    def _normalize_relation(cls, value: str) -> str:
        normalized = normalize_relation(str(value))
        if not RELATION_RE.fullmatch(normalized):
            raise ValueError(
                f"relation {value!r} cannot be normalized to UPPER_SNAKE_CASE"
            )
        return normalized


class ExtractionResult(BaseModel):
    """Triples extracted from one chunk."""

    model_config = ConfigDict(extra="forbid")

    triples: list[Triple] = Field(default_factory=list, max_length=100)


class Resolution(StrEnum):
    """How a mention was matched to a graph entity."""

    EXACT = "exact"  # same normalized key
    ALIAS = "alias"  # a stated alias or surface form matched
    LLM = "llm"  # the resolution model matched it to an existing entity
    NEW = "new"


class ResolvedEntity(BaseModel):
    """Where one extracted mention landed in the graph."""

    key: str  # canonical entity key the mention is merged into
    surface: str  # the name as written in the chunk
    method: Resolution = Resolution.NEW
    # The extracted mention (type, description, aliases, codes).
    ref: EntityRef | None = None


class GroundedTriple(BaseModel):
    """A triple bound to its chunk, mapped onto the ontology and resolved entities."""

    triple: Triple
    chunk_id: str
    source_id: str
    sensitivity: Sensitivity
    relation: str  # canonical relation actually written
    # Extracted predicate, when the ontology rewrote it.
    original_relation: str | None = None
    head: ResolvedEntity  # graph head (already swapped for inverse relations)
    tail: ResolvedEntity


@dataclass(frozen=True)
class SourceSwap:
    """What replacing a document changed in the graph."""

    triples_written: int
    chunks_removed: int
