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
from nexo.config import Settings
from nexo.llm import build_chat_model, build_embeddings, build_strands_model
from nexo.orchestration.graph import GraphDependencies, build_graph
from nexo.orchestration.state import ReviewDecision
from nexo.storage.graph_store import GraphStore
from nexo.storage.ingest import ExtractionAids, IngestionTargets, KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology
from nexo.storage.resolution import EntityResolver, ResolutionLimits
from nexo.storage.sensitive_data import build_detector
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore
from nexo.storage.vector_store import ChunkStore, LocalChunkStore
from nexo.telemetry import (
    caller_attributes,
    query_attributes,
    setup_telemetry,
    traced_span,
)

WORKFLOW_NAME = "nexo-investigation"


class RunResult(BaseModel):
    """Where a workflow thread stands after a start, review or status call."""

    thread_id: str
    status: str  # awaiting_review | completed
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
            final_response=values.get("final_response"),
            pending_review=pending,
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
        investigator = build_investigator(
            settings, graph_store, chunk_store_from_settings(settings, graph_store)
        )
        deps = GraphDependencies(
            settings=settings,
            investigator=investigator,
            detector=build_detector(
                settings.sensitive_data_provider, aws_region=settings.aws_region
            ),
            router_llm=build_chat_model(settings, role="router"),
        )
        yield Runtime(graph=build_graph(deps, checkpointer), settings=settings)


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
