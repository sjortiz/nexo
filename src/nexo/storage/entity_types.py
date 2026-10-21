"""Entity types a deployment extracts: a subset of the built-ins plus its own.

`ENTITY_TYPES_PATH` names a YAML or JSON file (example:
deploy/entity-types.example.yaml):

    builtin: [Person, Organization, Location, Product, Event, Concept]
    custom:
      - name: Contract
        description: a signed agreement between two parties

Unset, every built-in type is used and nothing is added. Each type's description
is shown to the extractor, and the extraction schema only accepts the configured
names (guided decoding), so a deployment's types stay in its own config.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexo.storage.schemas import (
    ENTITY_TYPE_DESCRIPTIONS,
    Attribute,
    EntityRef,
    EntityType,
    EntityTypeName,
    ExtractionResult,
    Triple,
)


class CustomType(BaseModel):
    """A deployment's own entity type."""

    model_config = ConfigDict(extra="forbid")

    name: EntityTypeName
    description: str = Field(min_length=1, max_length=300)


class EntityTypes(BaseModel):
    """The entity types one deployment extracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    builtin: list[EntityType] = Field(default_factory=lambda: list(EntityType))
    custom: list[CustomType] = Field(default_factory=list)

    @model_validator(mode="after")
    def _distinct(self) -> EntityTypes:
        names = [str(t) for t in self.builtin] + [c.name for c in self.custom]
        if len(names) != len(set(names)):
            raise ValueError(
                "entity type names must be unique (custom may not repeat built-ins)"
            )
        if not names:
            raise ValueError("configure at least one entity type")
        return self

    @classmethod
    def load(cls, path: Path) -> EntityTypes:
        """Load and validate an entity types file."""
        return cls.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        )

    @classmethod
    def from_settings(cls, settings: Any) -> EntityTypes:
        """The types ENTITY_TYPES_PATH configures, or every built-in when unset."""
        path = settings.entity_types_path
        return cls.load(path) if path else cls()

    @property
    def names(self) -> tuple[str, ...]:
        """Every configured type name, built-ins first."""
        return tuple(str(t) for t in self.builtin) + tuple(c.name for c in self.custom)

    def prompt_block(self) -> str:
        """One line per type for the extraction prompt: name and meaning."""
        lines = [f"  {t}: {ENTITY_TYPE_DESCRIPTIONS[t]}" for t in self.builtin]
        lines += [f"  {c.name}: {c.description}" for c in self.custom]
        return "\n".join(lines)

    @cached_property
    def extraction_schema(self) -> type[ExtractionResult]:
        """ExtractionResult whose entity types are limited to the configured names."""
        allowed = Literal[self.names]  # type: ignore[valid-type]

        # pylint: disable=missing-class-docstring  # local subclasses, same shape
        class Ref(EntityRef):
            type: allowed  # type: ignore[valid-type]

        class ConfiguredTriple(Triple):
            head: Ref
            tail: Ref

        class ConfiguredAttribute(Attribute):
            entity: Ref

        class ConfiguredExtraction(ExtractionResult):
            triples: list[ConfiguredTriple] = Field(
                default_factory=list, max_length=100
            )
            attributes: list[ConfiguredAttribute] = Field(
                default_factory=list, max_length=100
            )

        ConfiguredExtraction.__name__ = "ExtractionResult"
        return ConfiguredExtraction
