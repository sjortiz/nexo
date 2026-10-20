"""Composition root: builds real dependencies and drives the graph.

`open_runtime` / `open_ingestor` are the only places that touch live
infrastructure; everything else receives its dependencies, so tests swap in
fakes without patching.
"""

from __future__ import annotations

import importlib
import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel

from nexo.agents.investigator import Investigator
from nexo.agents.redaction import ChunkRedactor
from nexo.config import Settings
from nexo.llm import build_chat_model, build_embeddings, build_strands_model
from nexo.orchestration.graph import GraphDependencies, build_graph
from nexo.orchestration.state import ReviewDecision
from nexo.storage.graph_store import GraphStore
from nexo.storage.ingest import ExtractionAids, IngestionTargets, KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology
from nexo.storage.resolution import EntityResolver, ResolutionLimits
from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.sensitive_data import NoopDetector, build_detector
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore
from nexo.storage.vector_store import ChunkFilter, ChunkStore, LocalChunkStore
from nexo.telemetry import (
    caller_attributes,
    query_attributes,
    setup_telemetry,
    traced_span,
)

WORKFLOW_NAME = "nexo-investigation"
_INTERRUPT_CHANNEL = "__interrupt__"  # where LangGraph stores a pending interrupt


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
    pending_review: dict[str, Any] | None = None


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

    def start(
        self,
        user_query: str,
        *,
        thread_id: str | None = None,
        clearance: str | None = None,
        user: str | None = None,
    ) -> RunResult:
        """Run a new question until it completes or pauses for human review."""
        thread_id = thread_id or uuid.uuid4().hex
        payload = {
            "user_query": user_query,
            "request_id": thread_id,
            "caller_clearance": clearance or self.settings.default_clearance,
        }
        identity = caller_attributes(self.settings, user)
        identity |= query_attributes(self.settings, user_query)
        return self._drive(payload, thread_id, "start", identity)

    def review(
        self, thread_id: str, decision: ReviewDecision | dict[str, Any]
    ) -> RunResult:
        """Resume a thread paused for review with the reviewer's decision."""
        if isinstance(decision, ReviewDecision):
            decision = decision.model_dump(mode="json")
        if self._pending(thread_id) is None:
            raise ValueError(f"thread {thread_id} is not awaiting review")
        identity = caller_attributes(self.settings, decision.get("reviewer"))
        return self._drive(Command(resume=decision), thread_id, "review", identity)

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
        found: list[tuple[str, str]] = []  # (checkpoint timestamp, thread id)
        for item in saver.list(None):
            thread_id = item.config["configurable"]["thread_id"]
            if item.config["configurable"].get("checkpoint_ns") or thread_id in seen:
                continue
            seen.add(thread_id)
            if any(
                channel == _INTERRUPT_CHANNEL
                for _, channel, _ in item.pending_writes or []
            ):
                found.append((item.checkpoint["ts"], thread_id))
                if ordered and len(found) >= limit:
                    break
        found.sort(reverse=True)
        results = []
        for _, thread_id in found:
            pending = self._pending(thread_id)
            if pending is not None:  # the packet is the source of truth, not the scan
                results.append(self._result(thread_id, pending))
        return results[:limit]

    def review_passages(
        self, thread_id: str, clearance: Sensitivity | str
    ) -> list[dict[str, Any]]:
        """The cited chunks of a paused review, read as a caller with this clearance.

        One dict per citation, in citation order: `chunk_id`, `source`, `text`,
        `classification` and `status` (`shown`, `withheld` or `not_found`). `text` is
        None unless the status is `shown`; above the clearance it is masked by the
        ChunkRedactor (`[EMAIL]`, `[PHONE]`) when only detected personal details make
        the chunk sensitive, and otherwise withheld. Raises ValueError for a thread that is not
        awaiting review, and when the runtime has no chunk store.
        """
        pending = self._pending(thread_id)
        if pending is None:
            raise ValueError(f"thread {thread_id} is not awaiting review")
        if self.chunks is None:
            raise ValueError("this runtime has no chunk store")
        scope = ChunkFilter(max_sensitivity=Sensitivity(clearance))
        ids = list(dict.fromkeys(pending.get("citations") or []))
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
        # GenAI workflow span (OTel GenAI conventions): one per graph run; the
        # LangGraph thread id is the conversation id that ties start and review, and
        # identity says who asked (user.id, session.id) and which question.
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
                    if node == "__interrupt__":
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
            return result

    def _pending(self, thread_id: str) -> dict[str, Any] | None:
        snapshot = self.graph.get_state(self._config(thread_id))
        for t in snapshot.tasks:
            if t.interrupts:
                return t.interrupts[-1].value
        return None

    def _result(self, thread_id: str, pending: dict[str, Any] | None) -> RunResult:
        values = self.graph.get_state(self._config(thread_id)).values
        return RunResult(
            thread_id=thread_id,
            status="awaiting_review" if pending is not None else "completed",
            routing_target=values.get("routing_target"),
            routing_reason=values.get("routing_reason"),
            approval_status=values.get("approval_status"),
            outcome=_outcome(values, pending),
            final_response=values.get("final_response"),
            pending_review=pending,
        )


def _outcome(values: dict[str, Any], pending: dict[str, Any] | None) -> str | None:
    """Say how a thread ended, so a rejection never reads as plain success.

    `rejected: <reason>` is a system rejection (the router refused the question, or
    the run had no releasable output); `approved by` / `rejected by` name whoever
    decided: a reviewer or the review policy. Free text (the reviewer's comment, the
    router's reason) stays out: the CLI logs this line, and logs are exported
    (ADR-0009); the full result carries both.
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
) -> dict[str, Any]:
    """One review passage: as stored, masked by the redactor, or withheld."""
    if record is None:
        return {
            "chunk_id": chunk_id,
            "source": None,
            "text": None,
            "classification": None,
            "status": "not_found",
        }
    shown = redactor.visible(record, scope)
    return {
        "chunk_id": chunk_id,
        "source": " > ".join(
            p for p in [record.title or record.source_id, *record.section_path] if p
        ),
        "text": shown.text if shown else None,
        "classification": record.sensitivity.value,
        "status": "shown" if shown else "withheld",
    }


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
    ).with_detector(
        build_detector(settings.sensitive_data_provider, aws_region=settings.aws_region)
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
            detector=build_detector(
                settings.sensitive_data_provider, aws_region=settings.aws_region
            ),
            router_llm=build_chat_model(settings, role="router"),
        )
        yield Runtime(
            graph=build_graph(deps, checkpointer),
            settings=settings,
            chunks=chunk_store,
            redactor=ChunkRedactor(deps.detector),
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
        detector = build_detector(
            settings.sensitive_data_provider, aws_region=settings.aws_region
        )
        deps = GraphDependencies(
            settings=settings, investigator=_NoInvestigator(), detector=detector
        )
        yield Runtime(
            graph=build_graph(deps, checkpointer),
            settings=settings,
            chunks=chunks,
            redactor=ChunkRedactor(detector),
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
            detector=build_detector(
                settings.sensitive_data_provider, aws_region=settings.aws_region
            ),
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
