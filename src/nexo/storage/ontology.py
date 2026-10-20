"""Relation ontology: canonical predicates, synonyms, inverses and type constraints.

Free-form predicates drift (CO_FOUNDER vs COFOUNDED vs FOUNDED_BY), which
breaks multi-hop Cypher. With an ontology, every extracted predicate maps to
one canonical relation in one direction; anything that does not fit becomes
`RELATED_TO` with the original wording kept, so recall is not lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from nexo.storage.schemas import RELATION_RE, EntityType, normalize_relation

FALLBACK_RELATION = "RELATED_TO"


class RelationSpec(BaseModel):
    """One canonical relation with its synonyms, inverses and allowed types."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    source_types: list[EntityType] = Field(
        default_factory=list, description="Empty = any type"
    )
    target_types: list[EntityType] = Field(
        default_factory=list, description="Empty = any type"
    )
    synonyms: list[str] = Field(
        default_factory=list, description="Same meaning, same direction"
    )
    inverses: list[str] = Field(
        default_factory=list, description="Same meaning, head and tail swapped"
    )

    @field_validator("name", "synonyms", "inverses", mode="before")
    @classmethod
    def _normalize(cls, value):
        if isinstance(value, list):
            return [normalize_relation(v) for v in value]
        name = normalize_relation(value)
        if not RELATION_RE.fullmatch(name):
            raise ValueError(f"invalid relation name {value!r}")
        return name

    def allows(self, head: EntityType, tail: EntityType) -> bool:
        """Whether the head and tail types satisfy the type constraints."""
        return (not self.source_types or head in self.source_types) and (
            not self.target_types or tail in self.target_types
        )


@dataclass(frozen=True)
class RelationMapping:
    """Where an extracted predicate maps: relation, direction and original wording."""

    relation: str
    swap: bool  # True: store tail -> head
    original: str | None  # the extracted predicate when it was rewritten


class Ontology(BaseModel):
    """The set of canonical relations, indexed by every accepted name."""

    model_config = ConfigDict(extra="forbid")

    relations: list[RelationSpec]
    _by_name: dict[str, tuple[RelationSpec, bool]] = PrivateAttr(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Ontology:
        """Load and validate an ontology JSON file."""
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))

    @model_validator(mode="after")
    def _index_relations(self) -> Ontology:
        """Index every name, synonym and inverse; each may map to one relation only."""
        for spec in self.relations:
            for alias, swap in [
                (spec.name, False),
                *((s, False) for s in spec.synonyms),
                *((i, True) for i in spec.inverses),
            ]:
                if alias in self._by_name and self._by_name[alias][0] is not spec:
                    raise ValueError(f"{alias} maps to more than one relation")
                self._by_name[alias] = (spec, swap)
        return self

    def map(
        self, predicate: str, head: EntityType, tail: EntityType
    ) -> RelationMapping:
        """Canonical relation and direction for an extracted (head, predicate, tail)."""
        match = self._by_name.get(predicate)
        if match is not None:
            spec, swap = match
            first, second = (tail, head) if swap else (head, tail)
            if spec.allows(first, second):
                return RelationMapping(
                    spec.name, swap, None if predicate == spec.name else predicate
                )
            if spec.allows(second, first):  # extracted backwards
                return RelationMapping(spec.name, not swap, predicate)
        return RelationMapping(FALLBACK_RELATION, False, predicate)

    def prompt_block(self) -> str:
        """List the relations with their types and descriptions for the extraction prompt."""
        lines = []
        for spec in self.relations:
            source = "|".join(t.value for t in spec.source_types) or "Any"
            target = "|".join(t.value for t in spec.target_types) or "Any"
            lines.append(f"- {spec.name} ({source} -> {target}): {spec.description}")
        return "\n".join(lines)
