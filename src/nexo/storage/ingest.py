"""Module A - document ingestion: chunks -> vectors, triples -> Neo4j.

Pipeline for one document:

1. Lint, mark exclusion regions (`exclusions.py`), flatten tables,
   heading-aware chunking (`chunking.py`).
2. Sensitivity classification per chunk (declared floor + PII/PHI detector),
   optional redaction before anything is embedded or extracted.
3. Upsert chunks into the vector store under deterministic `chunk_id`s.
4. LLM triple extraction per chunk (bounded concurrency - vLLM's continuous
   batching turns parallel requests into throughput), Pydantic-validated.
   The tail of the previous chunk is shown as context so cross-chunk
   references resolve; evidence must still come from the chunk itself.
5. Drop triples whose evidence quote is not in the chunk (hallucination
   guard) or whose entity an exclusion rule names; turn a triple that made an
   amount an entity into an attribute (`AMOUNT_ENTITIES`); map predicates onto
   the ontology (synonyms, inverses, type constraints, RELATED_TO fallback).
6. Resolve entities against each other and the existing graph.
7. Replace the document's lexical + domain subgraph in one transaction.
8. Delete superseded vector chunks.

Steps 3 -> 7 -> 8 keep the invariant that every `chunk_id` in Neo4j resolves
in the vector store, even if the process dies between steps.
"""

from __future__ import annotations

import contextvars
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple, Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from nexo.llm import StructuredOutputError, invoke_structured
from nexo.storage.amounts import attribute_name, is_amount
from nexo.storage.chunking import (
    flatten_markdown_tables,
    glossary_notes,
    lint_document,
    split_document,
)
from nexo.storage.entity_types import EntityTypes
from nexo.storage.exclusions import (
    IngestExclusions,
    Marked,
    stage_texts,
    strip_markers,
)
from nexo.storage.ontology import FALLBACK_RELATION, Ontology
from nexo.storage.resolution import EntityResolver
from nexo.storage.schemas import (
    SourceSwap,
    Attribute,
    ChunkRecord,
    DocumentMetadata,
    EntityRef,
    ExtractionResult,
    GroundedAttribute,
    GroundedTriple,
    ResolvedEntity,
    Triple,
)
from nexo.storage.sensitive_data import SensitiveDataDetector, classify, redact
from nexo.storage.vector_store import ChunkStore
from nexo.telemetry import traced_span

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """\
You extract a knowledge graph from one passage of a document.

Return entity-relation-entity triples as JSON matching the schema.
- Entity types (pick the most specific; skip entities that fit none):
{entity_types}
- Use the most complete name for each entity ("Acme Holdings Inc.", not "the company" or "Acme"),
  including names introduced in the preceding text; resolve pronouns.
- aliases: other names the passage itself uses for the same entity (acronyms, short or former names).
- Predicates are short, specific, directional verbs in UPPER_SNAKE_CASE, oriented head -> tail.
  Avoid vague predicates like RELATED_TO.{relation_rule}
- Only facts the passage states. The preceding text is context only: never extract facts from it.
- Former roles and past relationships count as much as current ones ("previously led product at
  Delta Devices" is a Person-EMPLOYED_BY-Organization fact): extract them, with valid_at the date
  they held when the passage gives one.
- Dates, amounts, prices, ratings, percentages and other quantities are never entities: put a
  date the fact holds in valid_at, and a quantity in attributes, on the entity it describes
  (name: what is measured, value: as written with its unit, evidence: a verbatim quote).
- evidence: a verbatim quote (max ~200 characters) from the passage that states the fact.
- certainty: "asserted", "possible" (hedged: may, suspected) or "negated" (ruled out, denied).
- proposed_codes: only codes printed in the passage (ICD-10-CM, RxNorm, SNOMED CT, MeSH, UMLS).
- Return {{"triples": [], "attributes": []}} if the passage states no relationships.
"""


class GraphWriter(Protocol):  # pylint: disable=too-few-public-methods  # protocol
    """Graph write access ingestion needs."""

    def replace_source(
        self,
        document: DocumentMetadata,
        chunks: Sequence[ChunkRecord],
        triples: Sequence[GroundedTriple],
        attributes: Sequence[GroundedAttribute] = (),
    ) -> SourceSwap:
        """Replace everything the document contributed; say what changed."""


class IngestionReport(BaseModel):
    """What ingesting one document produced, dropped and warned about."""

    source_id: str
    chunk_count: int
    triples_extracted: int
    triples_written: int
    dropped: dict[str, int] = Field(default_factory=dict)
    converted: dict[str, int] = Field(
        default_factory=dict, description="triples turned into attributes, by reason"
    )
    attributes_written: int = 0
    excluded: dict[str, int] = Field(
        default_factory=dict, description="regions each exclusion rule matched"
    )
    relations: dict[str, int] = Field(
        default_factory=dict, description="canonical, remapped, fallback"
    )
    resolution: dict[str, int] = Field(
        default_factory=dict, description="mentions per resolution method"
    )
    failed_chunk_ids: list[str] = Field(default_factory=list)
    sensitivity: dict[str, int] = Field(default_factory=dict)
    stale_chunks_deleted: int = 0
    lint_warnings: list[str] = Field(default_factory=list)


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _direction(kept: _Kept) -> tuple[str, str]:
    """(head key, tail key) as the triple will be stored, after any ontology swap."""
    head, tail = kept.triple.head.key, kept.triple.tail.key
    return (tail, head) if kept.swap else (head, tail)


def _pair(kept: _Kept) -> tuple:
    """The chunk, canonical relation and unordered pair a triple is about."""
    return (kept.chunk.chunk_id, kept.relation, frozenset(_direction(kept)))


@dataclass(frozen=True)
class IngestionLimits:  # pylint: disable=too-many-instance-attributes  # one field per setting
    """Sizes and policies ingestion reads once; the service builds it from Settings."""

    chunk_size: int
    chunk_overlap: int
    extraction_attempts: int
    extraction_concurrency: int
    extraction_context_chars: int
    require_grounded_evidence: bool
    sensitive_data_policy: str
    amount_entities: str = "convert"

    @classmethod
    def from_settings(cls, settings: Any) -> IngestionLimits:
        """The limits the CHUNK_*, EXTRACTION_* and sensitive-data settings describe."""
        return cls(
            chunk_size=settings.chunk_size,
            chunk_overlap=settings.chunk_overlap,
            extraction_attempts=settings.extraction_attempts,
            extraction_concurrency=settings.extraction_concurrency,
            extraction_context_chars=settings.extraction_context_chars,
            require_grounded_evidence=settings.require_grounded_evidence,
            sensitive_data_policy=settings.sensitive_data_policy,
            amount_entities=settings.amount_entities,
        )


@dataclass
class _Counts:
    """What the extraction stages counted, assembled into the report once."""

    triples_extracted: int = 0
    failed_chunk_ids: list[str] = field(default_factory=list)
    dropped: Counter[str] = field(default_factory=Counter)
    converted: Counter[str] = field(default_factory=Counter)
    relations: Counter[str] = field(default_factory=Counter)
    resolution: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class IngestionTargets:
    """Where ingestion writes: `ingest()` needs both, `extract_triples()` neither."""

    chunk_store: ChunkStore | None
    graph_writer: GraphWriter | None


@dataclass(frozen=True)
class ExtractionAids:
    """Optional inputs that shape extraction and entity resolution."""

    glossary: Mapping[str, str] = field(default_factory=dict)
    ontology: Ontology | None = None
    resolver: EntityResolver | None = None
    callbacks: list = field(default_factory=list)
    exclusions: IngestExclusions | None = None
    entity_types: EntityTypes = field(default_factory=EntityTypes)


class _Kept(NamedTuple):
    """A grounded triple with its canonical relation, before entity resolution."""

    chunk: ChunkRecord
    triple: Triple
    relation: str
    swap: bool  # store tail -> head
    original: str | None  # the extracted predicate, when the ontology rewrote it


class _KeptAttribute(NamedTuple):
    """A grounded attribute, before entity resolution."""

    chunk: ChunkRecord
    attribute: Attribute


@dataclass
class _Prepared:
    """Chunks as stored, plus the text each stage sees of them (by chunk_id)."""

    chunks: list[ChunkRecord]
    warnings: list[str]
    search: dict[str, str]
    extraction: dict[str, str]
    excluded: Counter[str]

    def views(self, stage: str) -> list[ChunkRecord]:
        """The chunks with the text that stage sees."""
        texts = self.search if stage == "search" else self.extraction
        return [c.model_copy(update={"text": texts[c.chunk_id]}) for c in self.chunks]


_DROP = object()  # an amount triple to discard


class KnowledgeGraphIngestor:
    """Chunk, classify, extract, ground, resolve and write one document at a time."""

    def __init__(  # pylint: disable=too-many-arguments  # keyword-only wiring
        self,
        *,
        llm: BaseChatModel,
        detector: SensitiveDataDetector,
        targets: IngestionTargets,
        aids: ExtractionAids | None = None,
        limits: IngestionLimits | None = None,
        settings: Any = None,
    ) -> None:
        aids = aids or ExtractionAids()
        self._llm = llm
        self._detector = detector
        self._limits = limits or IngestionLimits.from_settings(settings)
        self._targets = targets
        self._aids = replace(
            aids,
            resolver=aids.resolver
            or EntityResolver(catalog=None, llm=None, mode="deterministic"),
        )
        self._config: RunnableConfig = {
            "callbacks": list(aids.callbacks),
            "tags": ["ingestion"],
        }
        relation_rule = (
            "\n- Prefer these relations (direction and types as shown); "
            "use another predicate only if none "
            f"fits:\n{aids.ontology.prompt_block()}"
            if aids.ontology
            else ""
        )
        self._system_prompt = EXTRACTION_SYSTEM_PROMPT.format(
            entity_types=aids.entity_types.prompt_block(),
            relation_rule=relation_rule,
        )

    # --- public API ---------------------------------------------------------------
    def ingest(self, text: str, metadata: DocumentMetadata) -> IngestionReport:
        """Ingest one document into the chunk store and graph, replacing its old version."""
        if self._targets.chunk_store is None or self._targets.graph_writer is None:
            raise ValueError(
                "ingest() needs a chunk store and a graph writer; "
                "use extract_triples() to extract without writing"
            )
        with traced_span(
            "ingest.document",
            {
                "document.source_id": metadata.source_id,
                "document.doc_type": metadata.doc_type,
            },
        ) as span:
            prepared = self._prepare(text, metadata)
            chunks = prepared.chunks
            self._targets.chunk_store.upsert(
                chunks,
                [
                    c.embedding_text(glossary_notes(c.text, self._aids.glossary))
                    for c in prepared.views("search")
                ],
            )

            grounded, attributes, counts = self._triples(prepared, resolve=True)
            swap = self._targets.graph_writer.replace_source(
                metadata, chunks, grounded, attributes
            )
            # The graph swap removes stale chunks itself when they live in Neo4j.
            stale = max(
                swap.chunks_removed,
                self._targets.chunk_store.delete_source(
                    metadata.source_id, keep={c.chunk_id for c in chunks}
                ),
            )
            span.set_attributes(
                {
                    "ingest.chunks": len(chunks),
                    "ingest.triples_written": swap.triples_written,
                    "ingest.failed_chunks": len(counts.failed_chunk_ids),
                }
            )
            return self._report(
                metadata,
                prepared,
                counts,
                triples_written=swap.triples_written,
                attributes_written=len(attributes),
                stale_chunks_deleted=stale,
                sensitivity=dict(Counter(c.sensitivity.value for c in chunks)),
            )

    def extract_triples(
        self, text: str, metadata: DocumentMetadata
    ) -> tuple[list[GroundedTriple], IngestionReport]:
        """Extract triples without writes or graph resolution, for evaluation against gold."""
        prepared = self._prepare(text, metadata)
        grounded, _, counts = self._triples(prepared, resolve=False)
        return grounded, self._report(metadata, prepared, counts)

    @staticmethod
    def _report(
        metadata: DocumentMetadata,
        prepared: _Prepared,
        counts: _Counts,
        **written: Any,
    ) -> IngestionReport:
        return IngestionReport(
            source_id=metadata.source_id,
            chunk_count=len(prepared.chunks),
            triples_extracted=counts.triples_extracted,
            triples_written=written.pop("triples_written", 0),
            dropped=dict(counts.dropped),
            converted=dict(counts.converted),
            excluded=dict(prepared.excluded),
            relations=dict(counts.relations),
            resolution=dict(counts.resolution),
            failed_chunk_ids=counts.failed_chunk_ids,
            lint_warnings=prepared.warnings,
            **written,
        )

    # --- stages ------------------------------------------------------------------------
    def _prepare(self, text: str, metadata: DocumentMetadata) -> _Prepared:
        """Chunks as stored, and what search and extraction see of each one.

        Exclusion regions are marked before chunking (so a region split across
        chunks is still recognised), then cut out per stage; the stored text and
        chunk id are those of the chunk without its marker lines.
        """
        warnings = lint_document(text)
        exclusions = self._aids.exclusions
        marked = (
            exclusions.mark(text, metadata.doc_type) if exclusions else Marked(text)
        )
        text, tables = flatten_markdown_tables(marked.text)
        if tables:
            warnings.append(
                f"{tables} table(s) flattened to bullets; prefer prose/lists at the source"
            )
        chunks = self._classify(
            split_document(
                text,
                metadata,
                chunk_size=self._limits.chunk_size,
                chunk_overlap=self._limits.chunk_overlap,
            )
        )
        raw = [c.text for c in chunks]
        stored = [
            c.model_copy(
                update={
                    "text": strip_markers(c.text),
                    "chunk_id": ChunkRecord.make_id(
                        c.source_id, c.ordinal, strip_markers(c.text)
                    ),
                }
            )
            for c in chunks
        ]
        ids = [c.chunk_id for c in stored]
        return _Prepared(
            chunks=stored,
            warnings=warnings,
            search=dict(zip(ids, stage_texts(raw, "search"), strict=True)),
            extraction=dict(zip(ids, stage_texts(raw, "extraction"), strict=True)),
            excluded=marked.matched,
        )

    def _classify(self, chunks: list[ChunkRecord]) -> list[ChunkRecord]:
        classified: list[ChunkRecord] = []
        for chunk in chunks:
            detections = self._detector.detect(chunk.text)
            level = classify(detections, chunk.sensitivity)
            text = chunk.text
            if detections and self._limits.sensitive_data_policy == "redact":
                text = redact(text, detections)
                # Residual risk is handled by the detector's recall.
                level = chunk.sensitivity
            classified.append(
                chunk.model_copy(
                    update={
                        "text": text,
                        "sensitivity": level,
                        "declared_sensitivity": chunk.sensitivity,
                        # Why the level is above the declared one (none when redacted).
                        "sensitivity_reasons": (
                            sorted(
                                {
                                    d.category
                                    for d in detections
                                    if d.sensitivity.rank > chunk.sensitivity.rank
                                }
                            )
                            if level.rank > chunk.sensitivity.rank
                            else []
                        ),
                        "chunk_id": ChunkRecord.make_id(
                            chunk.source_id, chunk.ordinal, text
                        ),
                    }
                )
            )
        return classified

    def _triples(
        self, prepared: _Prepared, *, resolve: bool
    ) -> tuple[list[GroundedTriple], list[GroundedAttribute], _Counts]:
        """Grounded triples and attributes on resolved entities, and what was counted."""
        counts = _Counts()
        # Chunks whose extraction text is empty (all excluded) cost no model call.
        views = [c for c in prepared.views("extraction") if c.text.strip()]
        kept, kept_attributes = self._grounded(views, counts)
        refs = [ref for k in kept for ref in (k.triple.head, k.triple.tail)]
        refs += [k.attribute.entity for k in kept_attributes]
        resolved = self._aids.resolver.resolve(refs) if resolve else {}
        grounded = _place(kept, resolved, counts.dropped)
        attributes = [
            GroundedAttribute(
                attribute=k.attribute,
                chunk_id=k.chunk.chunk_id,
                source_id=k.chunk.source_id,
                entity=_placed(k.attribute.entity, resolved),
            )
            for k in kept_attributes
        ]
        counts.resolution = Counter(e.method.value for e in resolved.values())
        return grounded, attributes, counts

    def _grounded(
        self, chunks: list[ChunkRecord], counts: _Counts
    ) -> tuple[list[_Kept], list[_KeptAttribute]]:
        """Grounded triples mapped onto the ontology, and grounded attributes.

        `chunks` carry the text extraction saw, so evidence is checked against it.
        """
        kept: list[_Kept] = []
        attributes: list[_KeptAttribute] = []
        for chunk, result in self._extract_all(chunks, counts.failed_chunk_ids):
            counts.triples_extracted += len(result.triples)
            haystack = _normalize(chunk.text)
            for triple in result.triples:
                rule = self._excluded_entity(chunk, triple.head, triple.tail)
                amount = self._amount_attribute(triple)
                if not self._is_grounded(triple.evidence, haystack):
                    counts.dropped["evidence_not_in_chunk"] += 1
                elif not self._types_enabled(triple.head, triple.tail):
                    counts.dropped["entity_type_not_enabled"] += 1
                elif rule is not None:
                    counts.dropped[f"excluded_entity:{rule}"] += 1
                elif amount is _DROP:
                    counts.dropped["amount_entity"] += 1
                elif amount is not None:
                    counts.converted["amount_entity"] += 1
                    attributes.append(_KeptAttribute(chunk, amount))
                elif triple.head.key == triple.tail.key:
                    counts.dropped["self_loop"] += 1
                else:
                    kept.append(self._mapped(chunk, triple, counts.relations))
            attributes += self._grounded_attributes(chunk, result.attributes, counts)
        return self._consistent(kept, counts.dropped), attributes

    def _grounded_attributes(
        self, chunk: ChunkRecord, extracted: list[Attribute], counts: _Counts
    ) -> list[_KeptAttribute]:
        """Attributes the model stated, with the same checks as triples."""
        haystack = _normalize(chunk.text)
        kept: list[_KeptAttribute] = []
        for attribute in extracted:
            rule = self._excluded_entity(chunk, attribute.entity)
            if not self._is_grounded(attribute.evidence, haystack):
                counts.dropped["attribute_evidence_not_in_chunk"] += 1
            elif not self._types_enabled(attribute.entity):
                counts.dropped["entity_type_not_enabled"] += 1
            elif rule is not None:
                counts.dropped[f"excluded_entity:{rule}"] += 1
            elif self._amounts_checked and is_amount(attribute.entity.name):
                counts.dropped["amount_entity"] += 1
            else:
                kept.append(_KeptAttribute(chunk, attribute))
        return kept

    def _is_grounded(self, evidence: str, haystack: str) -> bool:
        return (
            not self._limits.require_grounded_evidence
            or _normalize(evidence) in haystack
        )

    def _types_enabled(self, *refs: EntityRef) -> bool:
        """Whether every entity has a type this deployment allows (ENTITY_TYPES)."""
        return all(ref.type in self._aids.entity_types.names for ref in refs)

    def _excluded_entity(self, chunk: ChunkRecord, *refs: EntityRef) -> str | None:
        """The exclusion rule that names one of these entities, if any."""
        exclusions = self._aids.exclusions
        if exclusions is None:
            return None
        for ref in refs:
            rule = exclusions.excluded_entity(ref.name, chunk.doc_type)
            if rule is not None:
                return rule
        return None

    @property
    def _amounts_checked(self) -> bool:
        return self._limits.amount_entities != "keep"

    def _amount_attribute(self, triple: Triple) -> Attribute | object | None:
        """For a triple with an amount for an entity: its attribute, or _DROP.

        None when neither end is an amount (or AMOUNT_ENTITIES=keep). Converting
        needs exactly one amount end: the other end is the entity it describes.
        """
        if not self._amounts_checked:
            return None
        head, tail = is_amount(triple.head.name), is_amount(triple.tail.name)
        if not head and not tail:
            return None
        if self._limits.amount_entities == "drop" or head == tail:
            return _DROP
        entity, amount = (
            (triple.tail, triple.head) if head else (triple.head, triple.tail)
        )
        return Attribute(
            entity=entity,
            name=attribute_name(triple.relation),
            value=amount.name,
            evidence=triple.evidence,
        )

    def _consistent(self, kept: list[_Kept], dropped: Counter[str]) -> list[_Kept]:
        """Drop both sides when one chunk states a directional relation both ways.

        A ACQUIRED B next to B ACQUIRED A from the same passage is an extraction
        error, not two facts; neither direction is trusted. Relations the ontology
        gives no inverse to (PARTNERS_WITH) read the same both ways and stay.
        """
        ontology = self._aids.ontology
        ends: dict[tuple, set[tuple[str, str]]] = {}
        for k in kept:
            if ontology is not None and not ontology.directional(k.relation):
                continue
            ends.setdefault(_pair(k), set()).add(_direction(k))
        contradictory = {pair for pair, seen in ends.items() if len(seen) > 1}
        if not contradictory:
            return kept
        consistent = []
        for k in kept:
            if _pair(k) in contradictory:
                dropped["contradictory_direction"] += 1
            else:
                consistent.append(k)
        return consistent

    def _mapped(self, chunk: ChunkRecord, triple: Triple, stats: Counter[str]) -> _Kept:
        """The canonical relation and direction for a triple (unchanged without an ontology)."""
        ontology = self._aids.ontology
        if ontology is None:
            return _Kept(chunk, triple, triple.relation, False, None)
        mapping = ontology.map(triple.relation, triple.head.type, triple.tail.type)
        if mapping.relation == FALLBACK_RELATION:
            stats["fallback"] += 1
        else:
            stats["remapped" if mapping.original else "canonical"] += 1
        return _Kept(chunk, triple, mapping.relation, mapping.swap, mapping.original)

    def _extract_all(self, chunks: list[ChunkRecord], failed: list[str]):
        """Yield (chunk, extraction) per chunk; a chunk whose extraction fails goes to `failed`."""
        with ThreadPoolExecutor(
            max_workers=self._limits.extraction_concurrency
        ) as pool:
            # Each task runs in its own context copy so OTEL spans parent correctly.
            futures = [
                (
                    c,
                    pool.submit(
                        contextvars.copy_context().run,
                        self._extract,
                        c,
                        chunks[i - 1] if i else None,
                    ),
                )
                for i, c in enumerate(chunks)
            ]
            for chunk, future in futures:
                try:
                    yield chunk, future.result()
                except StructuredOutputError:
                    logger.warning(
                        "extraction failed validation for chunk %s", chunk.chunk_id
                    )
                    failed.append(chunk.chunk_id)
                # Any other failure (model unreachable after client retries, ...) costs
                # this chunk only, never the whole document.
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception("extraction failed for chunk %s", chunk.chunk_id)
                    failed.append(chunk.chunk_id)

    def _extract(
        self, chunk: ChunkRecord, previous: ChunkRecord | None
    ) -> ExtractionResult:
        section = chunk.source_label or "(none)"
        parts = [f"Document section: {section}"]
        limit = self._limits.extraction_context_chars
        if previous is not None and limit:
            parts.append(f"Preceding text (context only):\n...{previous.text[-limit:]}")
        parts.append(f"Passage:\n{chunk.text}")
        with traced_span("ingest.extract_triples", {"chunk.id": chunk.chunk_id}):
            return invoke_structured(
                self._llm,
                self._aids.entity_types.extraction_schema,
                [
                    SystemMessage(content=self._system_prompt),
                    HumanMessage(content="\n\n".join(parts)),
                ],
                max_attempts=self._limits.extraction_attempts,
                config=self._config,
            )


def _placed(ref: EntityRef, resolved: dict[str, ResolvedEntity]) -> ResolvedEntity:
    """Where a mention lands after resolution, carrying its surface form."""
    entry = resolved.get(ref.key) or ResolvedEntity(key=ref.key, surface=ref.name)
    return entry.model_copy(update={"surface": ref.name, "ref": ref})


def _place(
    kept: list[_Kept], resolved: dict[str, ResolvedEntity], dropped: Counter[str]
) -> list[GroundedTriple]:
    """Bind kept triples to resolved entities; drop those that became self-loops."""
    grounded: list[GroundedTriple] = []
    for chunk, triple, relation, swap, original in kept:
        head, tail = _placed(triple.head, resolved), _placed(triple.tail, resolved)
        if swap:
            head, tail = tail, head
        if head.key == tail.key:  # two names for one entity, now merged
            dropped["self_loop_after_resolution"] += 1
            continue
        grounded.append(
            GroundedTriple(
                triple=triple,
                chunk_id=chunk.chunk_id,
                source_id=chunk.source_id,
                sensitivity=chunk.sensitivity,
                relation=relation,
                original_relation=original,
                head=head,
                tail=tail,
            )
        )
    return grounded
