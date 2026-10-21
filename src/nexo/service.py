"""Composition root: builds real dependencies and drives the graph.

`open_runtime` / `open_ingestor` are the only places that touch live
infrastructure; everything else receives its dependencies, so tests swap in
fakes without patching.
"""

from __future__ import annotations

import importlib
import json
import logging
import uuid
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from nexo.agents.investigator import Investigator
from nexo.agents.redaction import ChunkRedactor
from nexo.config import Settings
from nexo.logging_setup import CONTENT_LOGGER
from nexo.llm import build_chat_model, build_embeddings, build_strands_model
from nexo.orchestration.graph import WORKFLOW_NAME, GraphDependencies, build_graph
from nexo.orchestration.state import ReviewDecision, ReviewPacket, ReviewPassage
from nexo.storage.graph_store import GraphStore
from nexo.storage.ingest import ExtractionAids, IngestionTargets, KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology
from nexo.storage.resolution import EntityResolver, ResolutionLimits
from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.sensitive_data import (
    NoopDetector,
    SensitiveDataDetector,
    build_detector,
)
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore
from nexo.storage.vector_store import ChunkFilter, ChunkStore, LocalChunkStore
from nexo.telemetry import (
    caller_attributes,
    query_attributes,
    setup_telemetry,
    traced_span,
)

CONTENT_EVENT = "nexo.content"
_INTERRUPT_CHANNEL = "__interrupt__"  # where LangGraph stores a pending interrupt

log = logging.getLogger("nexo.service")
# Its own logger, pinned to INFO by configure_logging: the audit record reaches OTLP
# whatever LOG_LEVEL is.
content_log = logging.getLogger(CONTENT_LOGGER)


class RunResult(BaseModel):
    """Where a workflow thread stands after a start, review or status call."""

    thread_id: str
    status: str  # awaiting_review | completed
    # How the thread ended, for people: e.g. "rejected: no releasable output". `status`
    # stays the lifecycle state, so a rejected run is still `completed`.
    outcome: str | None = None
    routing_target: str | None = None
    routing_reason: str | None = None
    approval_status: str | None = None
    final_response: str | None = None
    pending_review: ReviewPacket | None = None
    paused_at: datetime | None = None  # when the thread paused for review


@contextmanager
def open_checkpointer(settings: Settings) -> Iterator[BaseCheckpointSaver]:
    """Durable checkpoints are mandatory for human review: the pause outlives the process."""
    if settings.checkpoint_backend == "memory":
        yield InMemorySaver()
    elif settings.checkpoint_backend == "sqlite":
        settings.checkpoint_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with SqliteSaver.from_conn_string(
            str(settings.checkpoint_sqlite_path)
        ) as saver:
            yield saver
    else:
        try:  # optional extra: nexo[postgres]
            postgres = importlib.import_module("langgraph.checkpoint.postgres")
        except ModuleNotFoundError as exc:
            raise ValueError(
                "the postgres backend needs the extra: nexo[postgres]"
            ) from exc
        if settings.checkpoint_postgres_uri is None:
            raise ValueError(
                "CHECKPOINT_POSTGRES_URI is required for the postgres backend"
            )
        with postgres.PostgresSaver.from_conn_string(
            settings.checkpoint_postgres_uri.get_secret_value()
        ) as saver:
            saver.setup()
            yield saver


@dataclass
class Runtime:
    """Starts, resumes and inspects workflow threads on a compiled graph."""

    graph: CompiledStateGraph
    settings: Settings
    callbacks: list[Any] = field(default_factory=list)
    # review_passages reads cited chunks through the store and masks them with the redactor.
    chunks: ChunkStore | None = None
    redactor: ChunkRedactor = field(
        default_factory=lambda: ChunkRedactor(NoopDetector())
    )
    # Classifies the question and answer for the content gate; None withholds them.
    detector: SensitiveDataDetector | None = None

    def start(
        self,
        user_query: str,
        *,
        thread_id: str | None = None,
        clearance: str | None = None,
        user: str | None = None,
    ) -> RunResult:
        """Run a new question until it completes or, per the review policy, pauses for review."""
        thread_id = thread_id or uuid.uuid4().hex
        payload = {
            "user_query": user_query,
            "request_id": thread_id,
            "caller_clearance": clearance or self.settings.default_clearance,
        }
        identity = caller_attributes(self.settings, user)
        identity |= query_attributes(self.settings, user_query)
        return self._drive(payload, thread_id, "start", identity)

    def review(self, thread_id: str, decision: ReviewDecision) -> RunResult:
        """Resume a thread paused for review with the reviewer's decision."""
        if self._pending(thread_id) is None:
            raise ValueError(f"thread {thread_id} is not awaiting review")
        identity = caller_attributes(self.settings, decision.reviewer)
        resume = Command(resume=decision.model_dump(mode="json"))
        return self._drive(resume, thread_id, "review", identity)

    def status(self, thread_id: str) -> RunResult:
        """Return the thread's current result without running anything."""
        return self._result(thread_id, self._pending(thread_id))

    def pending_reviews(self, limit: int = 50) -> list[RunResult]:
        """Threads paused at the compliance interrupt, newest first.

        Each result carries the same review packet `status()` returns. Only the newest
        checkpoint of each thread counts, so a thread that was reviewed since it paused
        is not listed. The scan stops once `limit` paused threads are found.
        """
        saver = self.graph.checkpointer
        if not isinstance(saver, BaseCheckpointSaver) or limit < 1:
            return []
        # SQLite and Postgres list newest first across threads; the in-memory saver does not.
        ordered = not isinstance(saver, InMemorySaver)
        seen: set[str] = set()
        found: list[tuple[str, str, ReviewPacket]] = []  # (ts, thread, packet)
        before = None
        while len(found) < limit:
            # Scan for paused threads first and validate their packets after: the
            # saver holds its connection lock while listing, so get_state cannot run
            # inside the loop. An ordered saver is read a page at a time.
            candidates: list[tuple[str, str]] = []
            last = None
            more = False
            for item in saver.list(None, before=before):
                last = item.config
                thread_id = item.config["configurable"]["thread_id"]
                if (
                    item.config["configurable"].get("checkpoint_ns")
                    or thread_id in seen
                ):
                    continue
                seen.add(thread_id)
                if any(
                    channel == _INTERRUPT_CHANNEL
                    for _, channel, _ in item.pending_writes or []
                ):
                    candidates.append((item.checkpoint["ts"], thread_id))
                    if ordered and len(candidates) >= limit - len(found):
                        more = True
                        break
            for ts, thread_id in candidates:
                # The packet is the source of truth, not the scan: a thread whose
                # interrupt is not a review packet takes no slot from the limit.
                pending = self._pending(thread_id)
                if pending is not None:
                    found.append((ts, thread_id, pending))
            if not more or last is None:
                break
            before = last
        found.sort(key=lambda f: f[:2], reverse=True)
        return [
            self._result(thread_id, pending) for _, thread_id, pending in found[:limit]
        ]

    def review_passages(
        self, thread_id: str, clearance: Sensitivity | str
    ) -> list[ReviewPassage]:
        """The cited chunks of a paused review, read as a caller with this clearance.

        One passage per citation, in citation order. `text` is None unless the status
        is `shown`; above the clearance it is masked by the ChunkRedactor (`[EMAIL]`,
        `[PHONE]`) when only detected personal details make the chunk sensitive, and
        otherwise withheld. Raises ValueError for a thread that is not awaiting review,
        and when the runtime has no chunk store.
        """
        pending = self._pending(thread_id)
        if pending is None:
            raise ValueError(f"thread {thread_id} is not awaiting review")
        if self.chunks is None:
            raise ValueError("this runtime has no chunk store")
        scope = ChunkFilter(max_sensitivity=Sensitivity(clearance))
        ids = list(dict.fromkeys(pending.citations))
        records = {r.chunk_id: r for r in self.chunks.get_by_ids(ids)}
        return [
            _passage(chunk_id, records.get(chunk_id), scope, self.redactor)
            for chunk_id in ids
        ]

    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "callbacks": self.callbacks,
            "run_name": WORKFLOW_NAME,
            "metadata": {"thread_id": thread_id},
        }

    def _drive(
        self, payload: Any, thread_id: str, operation: str, identity: dict[str, Any]
    ) -> RunResult:
        # One workflow span per graph run; the thread id ties start and review (ADR-0009).
        attributes = {
            "gen_ai.operation.name": "invoke_workflow",
            "gen_ai.workflow.name": WORKFLOW_NAME,
            "gen_ai.conversation.id": thread_id,
            "nexo.operation": operation,
            **identity,
        }
        with traced_span(f"invoke_workflow {WORKFLOW_NAME}", attributes) as span:
            for update in self.graph.stream(
                payload, self._config(thread_id), stream_mode="updates"
            ):
                for node, delta in update.items():
                    if node == _INTERRUPT_CHANNEL:
                        span.add_event("graph.interrupt", {"node": "compliance"})
                    else:  # every state transition is visible on the request span
                        span.add_event(
                            "graph.state_update",
                            {"node": node, "keys": sorted(delta or {})},
                        )
            result = self._result(thread_id, self._pending(thread_id))
            span.set_attributes(
                {
                    "nexo.status": result.status,
                    "nexo.approval_status": result.approval_status or "",
                }
            )
            values = self.graph.get_state(self._config(thread_id)).values
            verdict, content = content_gate(self.settings, self.detector, values)
            span.set_attribute(CONTENT_EVENT, verdict)
            if content:
                span.add_event(CONTENT_EVENT, content)
            # Only the OTLP handler exports `content`; stderr gets the verdict.
            content_log.info(
                "thread %s: content: %s", thread_id, verdict, extra={"content": content}
            )
            return result

    def _pending(self, thread_id: str) -> ReviewPacket | None:
        snapshot = self.graph.get_state(self._config(thread_id))
        for t in snapshot.tasks:
            if t.interrupts:
                try:
                    return ReviewPacket.model_validate(t.interrupts[-1].value)
                except ValidationError as exc:
                    # One foreign or malformed interrupt must not break the whole
                    # pending list; the thread reads as not awaiting review.
                    log.warning(
                        "thread %s: interrupt is not a review packet (%d errors)",
                        thread_id,
                        exc.error_count(),
                    )
                    return None
        return None

    def _result(self, thread_id: str, pending: ReviewPacket | None) -> RunResult:
        snapshot = self.graph.get_state(self._config(thread_id))
        values = snapshot.values
        return RunResult(
            thread_id=thread_id,
            status="awaiting_review" if pending is not None else "completed",
            routing_target=values.get("routing_target"),
            routing_reason=values.get("routing_reason"),
            approval_status=values.get("approval_status"),
            outcome=_outcome(values, pending),
            final_response=values.get("final_response"),
            pending_review=pending,
            # The newest checkpoint of a paused thread is the pause itself.
            paused_at=snapshot.created_at if pending is not None else None,
        )


def _outcome(values: dict[str, Any], pending: ReviewPacket | None) -> str | None:
    """Say how a thread ended, so a rejection never reads as plain success.

    `rejected: <reason>` is a system rejection (the router refused the question, or
    the run had no releasable output); `approved by` / `rejected by` name whoever
    decided: a reviewer or the review policy. Free text (the reviewer's comment, the
    router's reason) stays out: the CLI logs this line, and logs are exported
    (ADR-0009); the full result carries both, and the run's content record carries
    them when its sensitivity allows (`content_gate`).
    """
    if pending is not None:
        return "awaiting review"
    if values.get("routing_target") == "rejected":
        return "rejected: router refused"
    status = values.get("approval_status")
    if status not in ("approved", "rejected"):
        return None
    review = values.get("review") or {}
    if _system_rejected(status, review):
        return "rejected: no releasable output"
    return f"{status} by {review.get('reviewer') or 'unknown reviewer'}"


def content_gate(
    settings: Settings, detector: SensitiveDataDetector | None, values: dict[str, Any]
) -> tuple[str, dict[str, str]]:
    """Whether a run's question and answer may be exported, and the attributes that carry them.

    Returns the verdict (`released (internal)`, `withheld (pii)`) and the texts to
    export: the question, the released answer, the reviewer's comment and the router's
    reason, or nothing when withheld. They are exported when the run's level (see
    `run_sensitivity`) is at or below TELEMETRY_CONTENT_MAX_SENSITIVITY; content
    capture exports them whatever the level. Fails closed: no detector (None, or the
    NoopDetector of SENSITIVE_DATA_PROVIDER=none, which cannot classify), or one that
    raises, withholds them (ADR-0009, amended).
    """
    texts = {
        "nexo.content.question": values.get("user_query"),
        "nexo.content.answer": values.get("final_response"),
        "nexo.content.review_comment": (values.get("review") or {}).get("comment"),
        "nexo.content.routing_reason": values.get("routing_reason"),
    }
    texts = {key: str(text) for key, text in texts.items() if text}
    if settings.capture_content:
        return "released (content capture on)", texts
    limit = settings.telemetry_content_max_sensitivity
    if limit == "none":
        return "withheld (telemetry content none)", {}
    if detector is None or isinstance(detector, NoopDetector):
        return "withheld (no detector)", {}
    try:
        level = run_sensitivity(values, detector, texts.values())
    except Exception:  # pylint: disable=broad-exception-caught  # fail closed
        log.warning("content withheld: the sensitive-data detector failed")
        return "withheld (detector failed)", {}
    if level.rank > Sensitivity(limit).rank:
        return f"withheld ({level})", {}
    return f"released ({level})", texts


def run_sensitivity(
    values: dict[str, Any], detector: SensitiveDataDetector, texts: Iterable[str]
) -> Sensitivity:
    """A run's level: the highest of the chunks its agent read and the detector's findings.

    The chunks are the evidence ledger (`fetched_sensitivity`) that grounds citations;
    the texts are what would be exported. A level Nexo does not rank counts as phi,
    and so does a failed agent run with no ledger: a timeout or crash drops what it read.
    """
    levels = [Sensitivity.PUBLIC]
    for output in values.get("agent_outputs") or []:
        fetched = output.get("fetched_sensitivity") or {}
        if output.get("error") and not fetched:
            levels.append(Sensitivity.PHI)
        levels += [_ranked(level) for level in fetched.values()]
    for text in texts:
        levels += [_ranked(d.sensitivity) for d in detector.detect(text)]
    return max(levels, key=lambda level: level.rank)


def _ranked(level: Any) -> Sensitivity:
    try:
        return Sensitivity(level)
    except ValueError:
        return Sensitivity.PHI


def _system_rejected(status: str, review: dict[str, Any]) -> bool:
    """Whether the compliance node rejected the run itself, not a reviewer."""
    if "source" in review:
        return review["source"] == "system"
    # Checkpoints from before the marker: the auto-reject had no other keys, while a
    # reviewer's decision always carries acknowledged_findings (even one named system).
    return (
        status == "rejected"
        and review.get("reviewer") == "system"
        and "acknowledged_findings" not in review
    )


def _passage(
    chunk_id: str,
    record: ChunkRecord | None,
    scope: ChunkFilter,
    redactor: ChunkRedactor,
) -> ReviewPassage:
    """One review passage: as stored, masked by the redactor, or withheld."""
    if record is None:
        return ReviewPassage(chunk_id=chunk_id, status="not_found")
    shown = redactor.visible(record, scope)
    return ReviewPassage(
        chunk_id=chunk_id,
        source=record.source_label,
        text=shown.text if shown else None,
        classification=record.sensitivity.value,
        status="shown" if shown else "withheld",
    )


def detector_from_settings(settings: Settings) -> SensitiveDataDetector:
    """The configured sensitive-data detector (regex by default)."""
    return build_detector(
        settings.sensitive_data_provider, aws_region=settings.aws_region
    )


def chunk_store_from_settings(
    settings: Settings, graph_store: GraphStore | None = None
) -> ChunkStore:
    """The configured chunk store (Neo4j by default), searched with the retrieval mode."""
    if settings.chunk_store == "neo4j":
        if graph_store is None:
            raise ValueError("the neo4j chunk store needs the graph store")
        return Neo4jChunkStore(
            graph_store,
            build_embeddings(settings),
            dimensions=settings.embedding_dimensions,
            mode=settings.retrieval_mode,
        )
    return LocalChunkStore(
        build_embeddings(settings),
        settings.vector_store_path,
        mode=settings.retrieval_mode,
    )


def build_investigator(
    settings: Settings, graph_store: GraphStore, chunk_store: ChunkStore
) -> Investigator:
    """The investigator agent over this graph and chunk store, on the agent model."""
    return Investigator(
        model=build_strands_model(settings),
        graph=graph_store,
        chunks=chunk_store,
        settings=settings,
        schema_text=graph_store.describe_schema(),
        detector=detector_from_settings(settings),
    )


@contextmanager
def open_runtime(settings: Settings) -> Iterator[Runtime]:
    """Yield a runtime wired to the configured checkpointer, graph store and models."""
    setup_telemetry(settings)
    with ExitStack() as stack:
        checkpointer = stack.enter_context(open_checkpointer(settings))
        graph_store = GraphStore.from_settings(settings, read_only=True)
        stack.callback(graph_store.close)
        chunk_store = chunk_store_from_settings(settings, graph_store)
        investigator = build_investigator(settings, graph_store, chunk_store)
        deps = GraphDependencies(
            settings=settings,
            investigator=investigator,
            detector=detector_from_settings(settings),
            router_llm=build_chat_model(settings, role="router"),
        )
        yield Runtime(
            graph=build_graph(deps, checkpointer),
            settings=settings,
            chunks=chunk_store,
            redactor=ChunkRedactor(deps.detector),
            detector=deps.detector,
        )


class _NoInvestigator:  # pylint: disable=too-few-public-methods
    """Stands in for the investigator in a runtime that only reviews."""

    def investigate(self, *args: Any, **kwargs: Any) -> Any:
        """Never called: a review-only runtime cannot start a question."""
        raise RuntimeError("this runtime only reviews; it cannot start investigations")


@contextmanager
def open_review_runtime(settings: Settings, chunks: ChunkStore) -> Iterator[Runtime]:
    """Yield a runtime that lists, reads and decides reviews, without models or the graph.

    It needs only the checkpointer, the detector and the chunk store, so a review server
    starts (and stays up) when Neo4j or the model host is down.
    """
    with ExitStack() as stack:
        checkpointer = stack.enter_context(open_checkpointer(settings))
        detector = detector_from_settings(settings)
        deps = GraphDependencies(
            settings=settings, investigator=_NoInvestigator(), detector=detector
        )
        yield Runtime(
            graph=build_graph(deps, checkpointer),
            settings=settings,
            chunks=chunks,
            redactor=ChunkRedactor(detector),
            detector=detector,
        )


@contextmanager
def open_ingestor(
    settings: Settings, *, writes: bool = True
) -> Iterator[KnowledgeGraphIngestor]:
    """Yield an ingestor wired to the configured stores, detector and extraction model."""
    setup_telemetry(settings)
    graph_store = GraphStore.from_settings(settings, read_only=False)
    try:
        graph_store.verify()
        chunk_store = chunk_store_from_settings(settings, graph_store)
        if writes:
            graph_store.ensure_schema()
            if isinstance(chunk_store, Neo4jChunkStore):
                chunk_store.ensure_schema()
        glossary = (
            json.loads(settings.glossary_path.read_text())
            if settings.glossary_path
            else {}
        )
        llm = build_chat_model(settings, role="extraction")
        yield KnowledgeGraphIngestor(
            llm=llm,
            detector=detector_from_settings(settings),
            settings=settings,
            targets=IngestionTargets(
                chunk_store=chunk_store,
                graph_writer=graph_store,
            ),
            aids=ExtractionAids(
                glossary=glossary,
                ontology=(
                    Ontology.load(settings.ontology_path)
                    if settings.ontology_path
                    else None
                ),
                resolver=EntityResolver(
                    catalog=graph_store,
                    llm=llm,
                    mode=settings.entity_resolution,
                    limits=ResolutionLimits(
                        max_candidates=settings.resolution_max_candidates,
                        batch_size=settings.resolution_batch_size,
                    ),
                ),
            ),
        )
    finally:
        graph_store.close()
