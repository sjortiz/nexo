"""Ingest exclusions: content kept out of chosen ingestion stages.

`INGEST_EXCLUSIONS_PATH` names a YAML or JSON file of rules (example:
deploy/ingest-exclusions.example.yaml). A rule selects lines of a document, or
entity names, and says which stages must not see them:

  document    removed before chunking: no stage sees it, citations included
  search      left out of the embedded and full-text indexed text
  extraction  left out of the text sent to triple extraction (and grounding)

A text region is found once over the whole document, then carried into the
chunks as marker lines (removed again before anything is stored or sent), so a
region split across two chunks is still excluded in both. Stored chunk text
(what citations read) keeps everything but `document` regions.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Stage = Literal["document", "search", "extraction"]

# Marker lines that open and close a region per marked stage. They must be
# printable (the Markdown header splitter drops non-printable characters) and
# short (they count towards chunk size); real text never has them on a line alone.
_MARKERS: dict[str, tuple[str, str]] = {
    "extraction": ("\u27e6x\u27e7", "\u27e6/x\u27e7"),
    "search": ("\u27e6s\u27e7", "\u27e6/s\u27e7"),
}
_ALL_MARKERS = frozenset(m for pair in _MARKERS.values() for m in pair)


def _compiled(pattern: str | None) -> re.Pattern[str] | None:
    return re.compile(pattern) if pattern is not None else None


class ExclusionRule(BaseModel):
    """One rule: what it selects, and which stages it is kept out of."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    stages: list[Stage] = Field(min_length=1)
    # Empty: every document; otherwise only documents of these doc types.
    doc_types: list[str] = Field(default_factory=list)
    # Exactly one selector: `lines`, `start` (with `while` or `end`), or `entity`.
    lines: str | None = None
    start: str | None = None
    while_: str | None = Field(default=None, alias="while")
    end: str | None = None
    entity: str | None = None

    @field_validator("lines", "start", "while_", "end", "entity")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regex {value!r}: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _one_selector(self) -> ExclusionRule:
        selectors = [s for s in (self.lines, self.start, self.entity) if s is not None]
        if len(selectors) != 1:
            raise ValueError(f"{self.name}: set exactly one of lines, start, entity")
        if (self.while_ is not None or self.end is not None) and self.start is None:
            raise ValueError(f"{self.name}: while/end need start")
        if self.while_ is not None and self.end is not None:
            raise ValueError(f"{self.name}: set while or end, not both")
        if self.entity is not None and set(self.stages) != {"extraction"}:
            raise ValueError(f"{self.name}: entity rules apply to extraction only")
        return self

    def applies_to(self, doc_type: str) -> bool:
        """Whether the rule covers documents of this type."""
        return not self.doc_types or doc_type in self.doc_types

    def regions(self, lines: list[str]) -> list[tuple[int, int]]:
        """[first, last) line ranges the rule selects."""
        found: list[tuple[int, int]] = []
        if self.lines is not None:
            pattern = re.compile(self.lines)
            return [(i, i + 1) for i, line in enumerate(lines) if pattern.search(line)]
        start = re.compile(self.start or "")
        cont, end = _compiled(self.while_), _compiled(self.end)
        i = 0
        while i < len(lines):
            if not start.search(lines[i]):
                i += 1
                continue
            j = i + 1
            if cont is not None:
                while j < len(lines) and cont.search(lines[j]):
                    j += 1
            elif end is not None:
                while j < len(lines) and not end.search(lines[j - 1]):
                    j += 1
            found.append((i, j))
            i = j
        return found


@dataclass
class Marked:
    """A document with `document` regions removed and the other regions marked."""

    text: str
    matched: Counter[str] = field(default_factory=Counter)


@dataclass
class _MarkPlan:
    """Lines to drop, and the markers to put before and after given lines."""

    drop: set[int] = field(default_factory=set)
    opens: dict[int, list[str]] = field(default_factory=dict)
    closes: dict[int, list[str]] = field(default_factory=dict)
    matched: Counter[str] = field(default_factory=Counter)

    def add(self, rule: ExclusionRule, first: int, last: int) -> None:
        """Plan one region [first, last) of a rule."""
        self.matched[rule.name] += 1
        if "document" in rule.stages:
            self.drop.update(range(first, last))
            return
        for stage in rule.stages:
            opening, closing = _MARKERS[stage]
            self.opens.setdefault(first, []).append(opening)
            self.closes.setdefault(last - 1, []).append(closing)


class IngestExclusions(BaseModel):
    """The rules of one exclusions file."""

    model_config = ConfigDict(extra="forbid")

    exclude: list[ExclusionRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> IngestExclusions:
        names = [r.name for r in self.exclude]
        if len(names) != len(set(names)):
            raise ValueError("exclusion rule names must be unique")
        return self

    @classmethod
    def load(cls, path: Path) -> IngestExclusions:
        """Load and validate a YAML or JSON exclusions file."""
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    @classmethod
    def from_settings(cls, settings: Any) -> IngestExclusions | None:
        """The rules INGEST_EXCLUSIONS_PATH configures, or None when unset."""
        path = settings.ingest_exclusions_path
        return cls.load(path) if path else None

    def mark(self, text: str, doc_type: str) -> Marked:
        """Remove `document` regions; wrap search/extraction regions in marker lines."""
        lines = text.split("\n")
        plan = _MarkPlan()
        for rule in self.exclude:
            if rule.entity is None and rule.applies_to(doc_type):
                for first, last in rule.regions(lines):
                    plan.add(rule, first, last)
        drop, opens, closes = plan.drop, plan.opens, plan.closes
        out: list[str] = []
        for i, line in enumerate(lines):
            if i in drop:
                continue
            out.extend(opens.get(i, []))
            out.append(line)
            out.extend(closes.get(i, []))
        return Marked("\n".join(out), plan.matched)

    def excluded_entity(self, name: str, doc_type: str) -> str | None:
        """The name of the first entity rule matching `name`, if any."""
        for rule in self.exclude:
            if (
                rule.entity is not None
                and rule.applies_to(doc_type)
                and re.search(rule.entity, name)
            ):
                return rule.name
        return None


def strip_markers(text: str) -> str:
    """Text with every marker line removed: the chunk as stored and cited."""
    return "\n".join(
        line for line in text.split("\n") if line.strip() not in _ALL_MARKERS
    )


def stage_texts(texts: list[str], stage: Literal["search", "extraction"]) -> list[str]:
    """What a stage sees of each chunk (in document order): its regions cut out.

    A chunk may start inside a region: when its first marker of this stage is a
    closing one, or, with no marker at all, when the previous chunk ended inside
    (a region longer than a chunk). An opening marker with no close cuts to the end.
    """
    opening, closing = _MARKERS[stage]
    out: list[str] = []
    inside = False
    for text in texts:
        lines = text.split("\n")
        own = [line.strip() for line in lines if line.strip() in (opening, closing)]
        if own:
            inside = own[0] == closing
        kept: list[str] = []
        for line in lines:
            marker = line.strip()
            if marker == opening:
                inside = True
            elif marker == closing:
                inside = False
            elif not inside and marker not in _ALL_MARKERS:
                kept.append(line)
        out.append("\n".join(kept))
    return out
