"""Entity resolution: map extracted mentions onto canonical graph entities.

Multi-hop questions only work if "Acme Holdings Inc.", "Acme" and "AHI" from
different documents land on one node. Resolution runs per entity type, in
cheap-to-expensive order, following the Claude Cookbook approach (block, then
let a model cluster with descriptions as tie-breakers):

1. Exact: same normalized key (`entity_key`).
2. Alias: a mention's name or stated alias equals another mention's or an
   existing entity's name/alias after normalization. Merges into the most
   complete (longest) name.
3. Model: remaining mentions are compared with token-blocked candidates from
   the graph by a guided-JSON arbiter. Unsure means "new" - over-merging a
   condition or medication is worse than a missed merge.

Every decision keeps the surface form and method, written on the
`MENTIONED_IN` edge, so merges are auditable and reversible.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from nexo.llm import invoke_structured
from nexo.storage.schemas import (
    EntityRef,
    EntityType,
    Resolution,
    ResolvedEntity,
    normalize_name,
)

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "and",
    "the",
    "of",
    "for",
    "inc",
    "corp",
    "group",
    "holdings",
    "company",
    "services",
    "de",
    "del",
    "la",
}


class Candidate(BaseModel):
    """An existing graph entity a mention might refer to."""

    key: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    description: str = ""


class EntityCatalog(Protocol):  # pylint: disable=too-few-public-methods  # protocol
    """Lookup of existing entities for resolution."""

    def find_candidates(
        self, entity_type: EntityType, tokens: Sequence[str], limit: int
    ) -> list[Candidate]:
        """Return same-type entities whose name or alias contains a token."""


class MatchDecision(BaseModel):
    """The resolution model's verdict for one mention."""

    mention: int = Field(description="Mention number from the list")
    match: str | None = Field(
        description="Key of the same real-world entity, or null if none is clearly the same"
    )


class MatchResult(BaseModel):
    """The resolution model's verdicts for a batch of mentions."""

    decisions: list[MatchDecision]


RESOLUTION_PROMPT = """\
You decide whether newly extracted {entity_type} mentions refer to the same real-world entity as an
existing entity. Use names, aliases and descriptions.

- Match only when they are clearly the same entity (different spelling, abbreviation, former name,
  short form). Related is not the same: a subsidiary is not its parent; a drug is not its class;
  "Type 2 diabetes" is not "diabetes"; two people sharing a surname are not the same person.
- When unsure, answer null. A wrong merge corrupts the graph; a missed merge only loses a link.
- Return one decision per mention, using the exact key shown for the match."""


def _tokens(text: str) -> set[str]:
    return {
        t
        for t in re.findall(r"[a-z0-9]+", text.casefold())
        if len(t) >= 3 and t not in _STOPWORDS
    }


@dataclass(frozen=True)
class ResolutionLimits:
    """How many existing entities to consider, and how many mentions per model call."""

    max_candidates: int = 25
    batch_size: int = 30


@dataclass
class _TypePass:
    """One entity type's mentions while they are being resolved."""

    entity_type: EntityType
    mentions: dict[str, EntityRef]  # exact key -> most complete mention
    names: dict[str, set[str]]  # exact key -> normalized name and aliases
    result: dict[str, ResolvedEntity]

    def roots(self) -> list[str]:
        """Mention keys not merged into another mention."""
        return [key for key in self.mentions if self.result[key].key == key]

    def tokens(self, keys: Sequence[str]) -> set[str]:
        """Blocking tokens of the given mentions' names and aliases."""
        return set().union(*(_tokens(" ".join(self.names[key])) for key in keys))


def _group_by_key(refs: Sequence[EntityRef]) -> dict[str, EntityRef]:
    """Group identical keys, keeping the longest surface name and every alias."""
    mentions: dict[str, EntityRef] = {}
    for ref in refs:
        current = mentions.get(ref.key)
        if current is None or len(ref.name) > len(current.name):
            mentions[ref.key] = ref.model_copy(
                update={
                    "aliases": sorted(
                        {*ref.aliases, *(current.aliases if current else [])}
                    ),
                    "description": (current.description if current else "")
                    or ref.description,
                }
            )
        else:
            current.aliases = sorted({*current.aliases, *ref.aliases})
    return mentions


def _merge_in_document_aliases(state: _TypePass) -> None:
    """Merge mentions that share a name or alias into the most complete name."""
    mentions, result = state.mentions, state.result
    for key in sorted(mentions, key=lambda k: -len(mentions[k].name)):
        if result[key].method is not Resolution.NEW:
            continue
        for other, mention in mentions.items():
            if (
                other != key
                and result[other].method is Resolution.NEW
                and state.names[other] & state.names[key]
                and len(mention.name) < len(mentions[key].name)
            ):
                result[other] = ResolvedEntity(
                    key=key, surface=mention.name, method=Resolution.ALIAS
                )


class EntityResolver:  # pylint: disable=too-few-public-methods  # resolve() is its purpose
    """Decide which existing entity, if any, each extracted mention refers to."""

    def __init__(
        self,
        *,
        catalog: EntityCatalog | None,
        llm: BaseChatModel | None,
        mode: str = "llm",
        limits: ResolutionLimits = ResolutionLimits(),
        config: RunnableConfig | None = None,
    ) -> None:
        self._catalog = catalog
        self._llm = llm
        self._mode = mode
        self._limits = limits
        self._config = config

    def resolve(self, mentions: Sequence[EntityRef]) -> dict[str, ResolvedEntity]:
        """Map each mention's exact key to where it should be written."""
        by_type: dict[EntityType, list[EntityRef]] = defaultdict(list)
        for ref in mentions:
            by_type[ref.type].append(ref)
        resolved: dict[str, ResolvedEntity] = {}
        for entity_type, refs in by_type.items():
            resolved.update(self._resolve_type(entity_type, refs))
        return resolved

    def _resolve_type(
        self, entity_type: EntityType, refs: list[EntityRef]
    ) -> dict[str, ResolvedEntity]:
        mentions = _group_by_key(refs)
        result = {
            key: ResolvedEntity(key=key, surface=ref.name)
            for key, ref in mentions.items()
        }
        if self._mode == "off":
            return result
        state = _TypePass(
            entity_type=entity_type,
            mentions=mentions,
            names={
                key: {normalize_name(ref.name, entity_type)}
                | {normalize_name(a, entity_type) for a in ref.aliases}
                for key, ref in mentions.items()
            },
            result=result,
        )
        _merge_in_document_aliases(state)
        candidates, unresolved = self._match_existing(state)
        if self._mode == "llm" and self._llm is not None and unresolved and candidates:
            self._arbitrate_unresolved(state, candidates, unresolved)
        return result

    def _match_existing(self, state: _TypePass) -> tuple[list[Candidate], list[str]]:
        """Exact and alias matches against graph entities; returns candidates and the rest."""
        roots = state.roots()
        if self._catalog is None or not roots:
            return [], []
        tokens = sorted(state.tokens(roots))
        if not tokens:
            return [], []
        prefix = f"{state.entity_type.value}:"
        candidates = [  # never merge across types, whatever the catalog returns
            c
            for c in self._catalog.find_candidates(
                state.entity_type,
                tokens,
                self._limits.max_candidates * max(1, len(roots)),
            )
            if c.key.startswith(prefix)
        ]
        by_name: dict[str, set[str]] = defaultdict(set)
        for candidate in candidates:
            for name in [candidate.name, *candidate.aliases]:
                by_name[normalize_name(name, state.entity_type)].add(candidate.key)

        unresolved: list[str] = []
        for key in roots:
            if any(c.key == key for c in candidates):
                self._redirect(state.result, key, key, Resolution.EXACT)
                continue
            hits = set().union(*(by_name.get(n, set()) for n in state.names[key]))
            # An alias shared by several entities is ambiguous: the model decides.
            if len(hits) == 1:
                self._redirect(state.result, key, hits.pop(), Resolution.ALIAS)
            else:
                unresolved.append(key)
        return candidates, unresolved

    def _arbitrate_unresolved(
        self, state: _TypePass, candidates: list[Candidate], unresolved: list[str]
    ) -> None:
        """Model arbitration in batches, only against candidates sharing a token."""
        size = self._limits.batch_size
        for start in range(0, len(unresolved), size):
            batch = unresolved[start : start + size]
            batch_tokens = state.tokens(batch)
            pool = [
                c
                for c in candidates
                if _tokens(" ".join([c.name, *c.aliases])) & batch_tokens
            ][: self._limits.max_candidates]
            if not pool:
                continue
            refs = [state.mentions[k] for k in batch]
            for key, match in self._arbitrate(
                state.entity_type, refs, batch, pool
            ).items():
                self._redirect(state.result, key, match, Resolution.LLM)

    @staticmethod
    def _redirect(
        result: dict[str, ResolvedEntity], key: str, target: str, method: Resolution
    ) -> None:
        for mention_key, entry in list(result.items()):
            if entry.key == key:  # the root and everything already merged into it
                result[mention_key] = ResolvedEntity(
                    key=target,
                    surface=entry.surface,
                    method=method if mention_key == key else entry.method,
                )

    def _arbitrate(
        self,
        entity_type: EntityType,
        refs: list[EntityRef],
        keys: list[str],
        pool: list[Candidate],
    ) -> dict[str, str]:
        existing = "\n".join(
            f"- key={c.key!r} name={c.name!r} aliases={c.aliases} description={c.description!r}"
            for c in pool
        )
        new = "\n".join(
            f"{i}. name={r.name!r} aliases={r.aliases} description={r.description!r}"
            for i, r in enumerate(refs)
        )
        try:
            result = invoke_structured(
                self._llm,
                MatchResult,
                [
                    SystemMessage(
                        content=RESOLUTION_PROMPT.format(entity_type=entity_type.value)
                    ),
                    HumanMessage(
                        content=f"Existing entities:\n{existing}\n\nNew mentions:\n{new}"
                    ),
                ],
                max_attempts=2,
                config=self._config,
            )
        # Resolution is an optimisation: no model failure, of any kind, may fail ingestion.
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning(
                "entity resolution model call failed; keeping %d mentions separate",
                len(refs),
                exc_info=True,
            )
            return {}
        allowed = {c.key for c in pool}
        decisions: dict[str, str] = {}
        for decision in result.decisions:
            if 0 <= decision.mention < len(keys) and decision.match in allowed:
                decisions[keys[decision.mention]] = decision.match
        return decisions
