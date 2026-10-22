"""Canonical names: one name and type per entity, whatever the extractor wrote.

`CANONICAL_NAMES_PATH` names a YAML or JSON file (keep a deployment's own in
local/, which git ignores; example: deploy/canonical-names.example.yaml):

    entities:
      - name: Acme Holdings
        type: Organization
        aliases: [Acme, AHI, Acme Holdings Inc.]

Before entity resolution, an extracted mention whose name matches a canonical
name or alias (case, spacing and punctuation folded, any type) is rewritten to
that name and type; the wording it had is kept as an alias. Entities of
different types never merge in resolution, so this is how a name the extractor
types inconsistently ends up as one node. `nexo entities merge` appends here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexo.storage.schemas import EntityRef, EntityTypeName, ShortText, normalize_name


def fold(name: str) -> str:
    """Comparison form of a name, independent of type: 'The Acme, Inc.' -> 'acme inc'."""
    return normalize_name(name, "")


class CanonicalEntity(BaseModel):
    """One entity's canonical name, type and the other names it goes by."""

    model_config = ConfigDict(extra="forbid")

    name: ShortText
    type: EntityTypeName
    aliases: list[ShortText] = Field(default_factory=list)


class CanonicalNames(BaseModel):
    """The canonical entities of one deployment."""

    model_config = ConfigDict(extra="forbid")

    entities: list[CanonicalEntity] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_owner_per_name(self) -> CanonicalNames:
        owners: dict[str, str] = {}
        for entity in self.entities:
            for name in (entity.name, *entity.aliases):
                key = fold(name)
                if owners.setdefault(key, entity.name) != entity.name:
                    raise ValueError(
                        f"{name!r} belongs to both {owners[key]!r} and {entity.name!r}"
                    )
        return self

    @classmethod
    def load(cls, path: Path) -> CanonicalNames:
        """Load a canonical names file; a missing or empty file means no entries."""
        if not path.exists():
            return cls()
        return cls.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        )

    @classmethod
    def from_settings(cls, settings: Any) -> CanonicalNames | None:
        """The names CANONICAL_NAMES_PATH configures, or None when unset."""
        path = settings.canonical_names_path
        return cls.load(path) if path else None

    def lookup(self, name: str) -> CanonicalEntity | None:
        """The canonical entity a name belongs to, if any."""
        key = fold(name)
        for entity in self.entities:
            if key in {fold(n) for n in (entity.name, *entity.aliases)}:
                return entity
        return None

    def apply(self, ref: EntityRef) -> EntityRef:
        """The mention under its canonical name and type (unchanged when unknown)."""
        entity = self.lookup(ref.name)
        if entity is None or (ref.name == entity.name and ref.type == entity.type):
            return ref
        aliases = list(dict.fromkeys([*ref.aliases, ref.name]))
        aliases = [a for a in aliases if a != entity.name][:10]
        return ref.model_copy(
            update={"name": entity.name, "type": entity.type, "aliases": aliases}
        )

    def record(self, name: str, entity_type: str, aliases: list[str]) -> None:
        """Add or extend an entry (used when merging entities)."""
        existing = self.lookup(name)
        if existing is not None and existing.name != name:
            name, entity_type = existing.name, existing.type
        for entity in self.entities:
            if entity.name == name:
                known = {fold(n) for n in (entity.name, *entity.aliases)}
                entity.aliases += [a for a in aliases if fold(a) not in known]
                entity.type = entity_type
                return
        others = [a for a in dict.fromkeys(aliases) if fold(a) != fold(name)]
        self.entities.append(
            CanonicalEntity(name=name, type=entity_type, aliases=others)
        )

    def save(self, path: Path) -> None:
        """Write the file back (YAML), creating its folder."""
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.model_dump(mode="json")
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
