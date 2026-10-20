"""The investigator's three tools, bound to one investigation's context.

Tools are built per investigation (closures over `InvestigationContext`) so
budget, clearance and the evidence ledger are per request and thread safe.
Expected failures (bad Cypher, unknown ids, budget exhausted) are returned to
the model as JSON errors it can act on; only programming errors raise.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired
from opentelemetry import trace
from strands import tool

from nexo.agents.models import InvestigationScope, ToolCallSummary
from nexo.config import Settings
from nexo.storage.graph_store import (
    ReadResult,
    UnsafeCypherError,
    validate_read_only_cypher,
)
from nexo.storage.schemas import CHUNK_ID_RE, ChunkRecord
from nexo.storage.vector_store import ChunkFilter


class GraphReader(Protocol):  # pylint: disable=too-few-public-methods  # protocol
    """Read-only Cypher access the query tool needs."""

    def run_read_query(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> ReadResult:
        """Run a read-only Cypher query and return its bounded rows."""


class ChunkReader(Protocol):  # pylint: disable=too-few-public-methods  # protocol
    """Chunk lookup and text search the chunk tools need."""

    def get_by_ids(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]:
        """Return the stored chunks for these ids; unknown ids are skipped."""

    def search(
        self, query: str, k: int, chunk_filter: ChunkFilter
    ) -> list[ChunkRecord]:
        """Return the top-k chunks the filter allows."""


@dataclass
class Evidence:
    """Chunk ids the agent found in graph rows, read, or was refused."""

    discovered: set[str] = field(default_factory=set)
    fetched: dict[str, str] = field(default_factory=dict)  # chunk_id -> sensitivity
    withheld: set[str] = field(default_factory=set)


@dataclass
class InvestigationContext:
    """Evidence ledger and budget for one investigation."""

    request_id: str
    scope: InvestigationScope
    max_tool_calls: int
    calls: list[ToolCallSummary] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve_call(self) -> bool:
        """Claim one tool call from the budget; False when it is exhausted."""
        # Strands may run tool calls from one model turn concurrently.
        with self._lock:
            if len(self.calls) >= self.max_tool_calls:
                return False
            self.calls.append(ToolCallSummary(tool="(reserved)", ok=False))
            return True

    def complete_call(self, summary: ToolCallSummary) -> None:
        """Replace a reserved slot with the finished call's summary."""
        with self._lock:
            for i, existing in enumerate(self.calls):
                if existing.tool == "(reserved)":
                    self.calls[i] = summary
                    return
            self.calls.append(summary)

    def record_discovered(self, chunk_ids: set[str]) -> None:
        """Chunk ids that appeared in graph rows."""
        with self._lock:
            self.evidence.discovered |= chunk_ids

    def record_fetched(self, sensitivity: dict[str, str], withheld: list[str]) -> None:
        """Chunks whose text the model received, and those withheld from it."""
        with self._lock:
            self.evidence.fetched.update(sensitivity)
            self.evidence.withheld.update(withheld)


def collect_chunk_ids(value: Any, found: set[str]) -> None:
    """Add every chunk id string nested anywhere in value to found."""
    if isinstance(value, str):
        if CHUNK_ID_RE.fullmatch(value):
            found.add(value)
    elif isinstance(value, dict):
        for item in value.values():
            collect_chunk_ids(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_chunk_ids(item, found)


def redact_above_clearance(value: Any, clearance_rank: int) -> Any:
    """Redact node/relationship maps whose sensitivity_rank exceeds the caller's clearance.

    Only catches values projected as whole nodes; scalar projections
    (RETURN e.name) need database-side RBAC on the :PII/:PHI labels.
    """
    if isinstance(value, dict):
        if int(value.get("sensitivity_rank", 0) or 0) > clearance_rank:
            return {"redacted": "above caller clearance"}
        return {k: redact_above_clearance(v, clearance_rank) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_above_clearance(v, clearance_rank) for v in value]
    return value


def _dump(payload: dict[str, Any], max_chars: int) -> str:
    """Serialise, dropping trailing rows/chunks (never cutting JSON) to fit the budget."""
    text = json.dumps(payload, default=str, ensure_ascii=False)
    for key in ("rows", "chunks"):
        items = payload.get(key)
        while len(text) > max_chars and isinstance(items, list) and items:
            items.pop()
            payload["omitted_for_size"] = payload.get("omitted_for_size", 0) + 1
            text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > max_chars:
        return json.dumps(
            {"error": "result too large; return fewer or smaller properties"}
        )
    return text


def build_investigation_tools(
    *,
    graph: GraphReader,
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
) -> list[Any]:
    """Build the graph query, chunk fetch and chunk search tools for this investigation."""
    return [
        _query_tool(graph, ctx, settings),
        _fetch_tool(chunks, ctx, settings),
        _search_tool(chunks, ctx, settings),
    ]


def _query_tool(
    graph: GraphReader, ctx: InvestigationContext, settings: Settings
) -> Any:
    """The graph query tool."""
    clearance = ctx.scope.chunk_filter.max_sensitivity
    max_chars = settings.tool_result_max_chars

    @tool
    def query_knowledge_graph(cypher_query: str) -> str:
        """Run one read-only Cypher query against the Neo4j knowledge graph.

        Use it to trace multi-hop relationships. Bound variable-length paths
        (for example -[*1..3]-) and always LIMIT results. Return the chunk_id or
        chunk_ids properties of the nodes and relationships you care about so
        their source text can be fetched. The result lists every chunk id it saw
        under "chunk_ids_found".

        Args:
            cypher_query: A single read-only Cypher statement (MATCH, OPTIONAL
                MATCH, WITH, WHERE, RETURN, UNWIND, CALL subqueries). Writes and
                procedure calls are rejected.
        """
        if not ctx.reserve_call():
            return json.dumps(
                {
                    "error": "tool budget exhausted; write your answer from the evidence gathered"
                }
            )
        started = time.perf_counter()
        # Strands' `execute_tool query_knowledge_graph` span is current here; the
        # Neo4j CLIENT span is a child created by GraphStore.
        span = trace.get_current_span()
        try:
            query = validate_read_only_cypher(cypher_query)
            result = graph.run_read_query(query)
        except UnsafeCypherError as exc:
            span.set_attribute("error.type", "unsafe_cypher")
            return _finish(
                ctx,
                _summary("query_knowledge_graph", started, False, str(exc)),
                {"error": str(exc), "hint": "rewrite as a single read-only query"},
            )
        except (ServiceUnavailable, SessionExpired):
            span.set_attribute("error.type", "graph_unavailable")
            return _finish(
                ctx,
                _summary("query_knowledge_graph", started, False, "graph unavailable"),
                {
                    "error": "knowledge graph unavailable; do not retry, report what you have"
                },
            )
        except Neo4jError as exc:  # syntax errors, timeouts, access-mode violations
            span.set_attribute("error.type", exc.code or "neo4j_error")
            return _finish(
                ctx,
                _summary(
                    "query_knowledge_graph", started, False, exc.code or "neo4j error"
                ),
                {"error": exc.message or str(exc), "code": exc.code},
            )
        rows = redact_above_clearance(result.rows, clearance.rank)
        found: set[str] = set()
        collect_chunk_ids(rows, found)
        ctx.record_discovered(found)
        span.set_attributes(
            {
                "nexo.graph.rows": len(rows),
                "nexo.graph.truncated": result.truncated,
                "nexo.graph.chunk_ids_found": len(found),
            }
        )
        payload = {
            "row_count": len(rows),
            "truncated": result.truncated,
            "rows": rows,
            "chunk_ids_found": sorted(found),
        }
        return _finish(
            ctx,
            _summary("query_knowledge_graph", started, True, f"{len(rows)} rows"),
            payload,
            max_chars,
        )

    return query_knowledge_graph


def _fetch_tool(
    chunks: ChunkReader, ctx: InvestigationContext, settings: Settings
) -> Any:
    """The chunk fetch tool."""
    max_chars = settings.tool_result_max_chars

    @tool
    def fetch_vector_chunks(chunk_ids: list[str]) -> str:
        """Fetch the exact source text for chunk ids found in the knowledge graph.

        This is the evidence: cite facts as [chunk:<chunk_id>] using only chunks
        returned here or given to you as seed passages. Chunks outside the
        caller's clearance or scope are withheld.

        Args:
            chunk_ids: Chunk ids (format chk_ followed by 16 hex characters) from graph results.
        """
        if not ctx.reserve_call():
            return json.dumps(
                {
                    "error": "tool budget exhausted; write your answer from the evidence gathered"
                }
            )
        started = time.perf_counter()
        requested = list(dict.fromkeys(chunk_ids))
        well_formed = [c for c in requested if CHUNK_ID_RE.fullmatch(c)]
        valid = well_formed[: settings.agent_max_chunks_per_call]
        # Strands' `execute_tool fetch_vector_chunks` span
        span = trace.get_current_span()
        try:
            records = chunks.get_by_ids(valid)
        # Any store failure (down, corrupt) goes back to the agent; the trace keeps it.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            return _finish(
                ctx,
                _summary("fetch_vector_chunks", started, False, type(exc).__name__),
                {"error": "vector store unavailable; report what you have"},
            )
        returned, withheld = [], []
        sensitivity = {r.chunk_id: r.sensitivity.value for r in records}
        for record in records:
            if not ctx.scope.chunk_filter.allows(record):
                withheld.append(record.chunk_id)
                continue
            returned.append(_chunk_view(record))
        payload = {
            "chunks": returned,
            "withheld": withheld,
            "not_found": sorted(set(valid) - set(sensitivity)),
            "invalid_ids": [c for c in requested if c not in well_formed],
            "over_limit_not_fetched": well_formed[len(valid) :],
        }
        text = _finish(
            ctx,
            _summary("fetch_vector_chunks", started, True, f"{len(returned)} chunks"),
            payload,
            max_chars,
        )
        # Only text the model actually received counts as evidence.
        ctx.record_fetched(
            {c["chunk_id"]: sensitivity[c["chunk_id"]] for c in payload["chunks"]},
            withheld,
        )
        span.set_attributes(
            {
                "nexo.chunks.requested": len(valid),
                "nexo.chunks.returned": len(payload["chunks"]),
                "nexo.chunks.withheld": len(withheld),
            }
        )
        return text

    return fetch_vector_chunks


def _search_tool(
    chunks: ChunkReader, ctx: InvestigationContext, settings: Settings
) -> Any:
    """The chunk text search tool."""
    max_chars = settings.tool_result_max_chars

    @tool
    def search_chunks(query: str, k: int = 5) -> str:
        """Search the source document text for passages that match a query.

        Use it when the graph has no matching entity or path: extraction can miss
        entities and relationships that the text states. Search with the names and
        key words from the question (hybrid keyword and semantic ranking). Results
        are evidence like fetched chunks: cite them as [chunk:<chunk_id>]. Only
        passages inside the caller's clearance and scope are searched.

        Args:
            query: Entity names and key words, for example "Delta Devices".
            k: How many passages to return (at most the per-call chunk limit).
        """
        if not ctx.reserve_call():
            return json.dumps(
                {
                    "error": "tool budget exhausted; write your answer from the evidence gathered"
                }
            )
        started = time.perf_counter()
        limit = max(1, min(k, settings.agent_max_chunks_per_call))
        # Strands' `execute_tool search_chunks` span is current here.
        span = trace.get_current_span()
        try:
            records = chunks.search(query, limit, ctx.scope.chunk_filter)
        # Any store failure (down, corrupt) goes back to the agent; the trace keeps it.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            return _finish(
                ctx,
                _summary("search_chunks", started, False, type(exc).__name__),
                {"error": "vector store unavailable; report what you have"},
            )
        # The store filters before ranking; check again so the ledger never
        # records text the caller may not see.
        allowed = [r for r in records if ctx.scope.chunk_filter.allows(r)]
        payload = {
            "query": query,
            "chunks": [_chunk_view(r) for r in allowed],
        }
        text = _finish(
            ctx,
            _summary("search_chunks", started, True, f"{len(allowed)} chunks"),
            payload,
            max_chars,
        )
        sensitivity = {r.chunk_id: r.sensitivity.value for r in allowed}
        ctx.record_fetched(
            {c["chunk_id"]: sensitivity[c["chunk_id"]] for c in payload["chunks"]},
            [],
        )
        span.set_attributes(
            {
                "nexo.chunks.requested": limit,
                "nexo.chunks.returned": len(payload["chunks"]),
            }
        )
        return text

    return search_chunks


def _chunk_view(record: ChunkRecord) -> dict[str, Any]:
    """What the model sees of a chunk: id, source path, date and exact text."""
    parts = [record.title or record.source_id, *record.section_path]
    return {
        "chunk_id": record.chunk_id,
        "source": " > ".join(p for p in parts if p),
        "effective_date": record.effective_date,
        "text": record.text,
    }


def _summary(name: str, started: float, ok: bool, detail: str) -> ToolCallSummary:
    """The audit record of one tool call."""
    return ToolCallSummary(
        tool=name,
        ok=ok,
        detail=detail[:200],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def _finish(
    ctx: InvestigationContext,
    summary: ToolCallSummary,
    payload: dict[str, Any],
    max_chars: int = 4000,
) -> str:
    """Record the call and return its payload as JSON for the model."""
    ctx.complete_call(summary)
    return _dump(payload, max_chars)
