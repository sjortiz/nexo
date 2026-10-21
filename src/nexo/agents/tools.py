"""The investigator's tools, bound to one investigation's context.

Tools are built per investigation (closures over `InvestigationContext`) so
budget, clearance and the evidence ledger are per request and thread safe.
Expected failures (bad Cypher, unknown keys, budget exhausted) raise
`ToolFailure` inside a tool; `_run_tool` records them and returns them to the
model as JSON errors it can act on. Only programming errors escape.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired
from opentelemetry import trace
from strands import tool
from strands.types.tools import ToolResult

from nexo.agents.models import InvestigationScope, ToolCallSummary
from nexo.agents.redaction import ChunkRedactor
from nexo.config import Settings
from nexo.storage.graph_store import (
    GraphReader,
    ReadResult,
    UnsafeCypherError,
    validate_read_only_cypher,
)
from nexo.storage.schemas import CHUNK_ID_RE, ChunkRecord, Sensitivity
from nexo.storage.sensitive_data import NoopDetector
from nexo.storage.vector_store import ChunkFilter

log = logging.getLogger(__name__)


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


_RESERVED = "(reserved)"


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
            self.calls.append(ToolCallSummary(tool=_RESERVED, ok=False))
            return True

    def calls_left(self) -> int:
        """Tool calls still available in the budget."""
        with self._lock:
            return max(self.max_tool_calls - len(self.calls), 0)

    def complete_call(self, summary: ToolCallSummary) -> None:
        """Replace a reserved slot with the finished call's summary."""
        with self._lock:
            for i, existing in enumerate(self.calls):
                if existing.tool == _RESERVED:
                    self.calls[i] = summary
                    return
            self.calls.append(summary)

    def record_refused(self, summary: ToolCallSummary) -> None:
        """Record a call refused because the budget was spent; it holds no slot."""
        with self._lock:
            self.calls.append(summary)

    def completed_calls(self) -> list[ToolCallSummary]:
        """The finished calls, without slots still reserved by running ones."""
        with self._lock:
            return [c for c in self.calls if c.tool != _RESERVED]

    def record_discovered(self, chunk_ids: set[str]) -> None:
        """Chunk ids that appeared in graph rows."""
        with self._lock:
            self.evidence.discovered |= chunk_ids

    def record_fetched(self, sensitivity: dict[str, str], withheld: list[str]) -> None:
        """Chunks whose text the model received, and those withheld from it."""
        with self._lock:
            self.evidence.fetched.update(sensitivity)
            self.evidence.withheld.update(withheld)


def collect_chunk_ids(value: Any) -> set[str]:
    """Every chunk id string nested anywhere in value."""
    if isinstance(value, str):
        return {value} if CHUNK_ID_RE.fullmatch(value) else set()
    if isinstance(value, dict):
        return collect_chunk_ids(list(value.values()))
    if isinstance(value, (list, tuple)):
        return set().union(*(collect_chunk_ids(item) for item in value))
    return set()


# Chunk text is served only by the chunk tools, through the redactor; graph
# rows are for structure. Neo4jChunkStore keeps these on the (:Chunk) nodes.
CHUNK_TEXT_KEYS = frozenset({"text", "search_text", "embedding"})
_UNKNOWN_RANK = Sensitivity.PHI.rank


def _rank(value: dict[str, Any]) -> int:
    """The map's sensitivity rank; a level that cannot be read counts as phi.

    Entities and edges carry `sensitivity_rank`, chunks and documents a
    `sensitivity` level. A rank projected but never set (null) and MENTIONED_IN
    maps (a verbatim `surface`, no rank) count as phi.
    """
    rank = value.get("sensitivity_rank", 0)
    rank = _UNKNOWN_RANK if rank is None else int(rank)
    level = value.get("sensitivity")
    if level is not None:
        try:
            rank = max(rank, Sensitivity(level).rank)
        except ValueError:
            rank = _UNKNOWN_RANK
    if "surface" in value and "sensitivity_rank" not in value:
        rank = max(rank, _UNKNOWN_RANK)
    return rank


def _hidden_key(value: dict[str, Any], key: str, clearance_rank: int) -> bool:
    """True for chunk text, and for an edge's evidence quoted from above clearance.

    An edge's `sensitivity_rank` is its least sensitive source, but `evidence`
    is verbatim text from one chunk, ranked by `evidence_rank`; an edge written
    before that rank existed has none, so its evidence is hidden.
    """
    if key in CHUNK_TEXT_KEYS:
        return True
    if key == "evidence":
        rank = value.get("evidence_rank")
        return not isinstance(rank, int) or rank > clearance_rank
    return False


def redact_above_clearance(value: Any, clearance_rank: int) -> Any:
    """Redact node/relationship maps above the caller's clearance; drop chunk text.

    A map is redacted whole when its rank (see `_rank`) exceeds the clearance.
    Chunk text, search text and embeddings are dropped from every map whatever
    the clearance, and an edge's `evidence` unless its `evidence_rank` is within
    it. Only catches values projected as maps; scalar projections (RETURN
    e.name) need database-side RBAC on the :PII/:PHI labels.
    """
    if isinstance(value, dict):
        if _rank(value) > clearance_rank:
            return {"redacted": "above caller clearance"}
        return {
            k: redact_above_clearance(v, clearance_rank)
            for k, v in value.items()
            if not _hidden_key(value, k, clearance_rank)
        }
    if isinstance(value, (list, tuple)):  # the driver returns relationships as tuples
        return [redact_above_clearance(v, clearance_rank) for v in value]
    return value


# Entity-to-Entity hops only: lexical edges (MENTIONED_IN, NEXT_CHUNK, ...) end at
# Chunk or Document nodes. Hop and path counts are validated ints, inlined
# because Cypher takes no parameters there. Only RELATED_TO edges have a predicate
# and only dated edges a valid_at; reading them through properties(r) avoids
# Neo4j's "property does not exist" warning on a graph that has none.
_PATHS = """\
MATCH (a:Entity {{key: $from_key}}), (b:Entity {{key: $to_key}})
OPTIONAL MATCH p = SHORTEST {paths} (a)(()-[]-(:Entity)){{1,{hops}}}(b)
RETURN [n IN nodes(p) | n {{.key, .name, .type, .sensitivity_rank}}] AS nodes,
       [r IN relationships(p) | r {{from: startNode(r).key, relation: type(r), to: endNode(r).key,
           predicate: properties(r)['predicate'], valid_at: properties(r)['valid_at'],
           .certainty, .evidence, .evidence_rank, .chunk_ids,
           sensitivity_rank: coalesce(r.sensitivity_rank, {unranked})}}] AS hops"""

_NEIGHBOURS = f"""\
MATCH (e:Entity {{key: $key}})
OPTIONAL MATCH (e)-[r]-(n:Entity)
WITH e, r, n ORDER BY size(coalesce(r.chunk_ids, [])) DESC LIMIT $limit
RETURN e {{.key, .name, .type, .sensitivity_rank}} AS entity,
       r {{relation: type(r), outgoing: startNode(r) = e, predicate: properties(r)['predicate'],
          valid_at: properties(r)['valid_at'], .certainty, .evidence, .evidence_rank, .chunk_ids,
          sensitivity_rank: coalesce(r.sensitivity_rank, {_UNKNOWN_RANK})}} AS edge,
       n {{.key, .name, .type, .sensitivity_rank}} AS neighbour"""

_UNKNOWN_KEY = {
    "error": "unknown entity key; look entities up by name and alias with "
    "query_knowledge_graph, or search_chunks for the text"
}

_BUDGET_EXHAUSTED = {
    "error": "tool budget exhausted; write your answer from the evidence gathered"
}

_CHUNK_STORE_DOWN = {"error": "chunk store unavailable; report what you have"}

_UNEXPECTED = {"error": "the tool failed unexpectedly; report what you have"}


def _is_redacted(value: Any) -> bool:
    """True if value holds anything redact_above_clearance replaced."""
    if isinstance(value, dict):
        return "redacted" in value or any(_is_redacted(v) for v in value.values())
    if isinstance(value, list):
        return any(_is_redacted(v) for v in value)
    return False


def visible_paths(rows: list[dict[str, Any]], clearance_rank: int) -> dict[str, Any]:
    """The found paths within clearance; one through anything above it would reveal it exists."""
    found = [redact_above_clearance(row, clearance_rank) for row in rows if row["hops"]]
    visible = [p for p in found if not _is_redacted(p)]
    return {
        "path_count": len(visible),
        "withheld": len(found) - len(visible),
        "paths": visible,
    }


def visible_neighbours(
    rows: list[dict[str, Any]], clearance_rank: int
) -> dict[str, Any] | None:
    """The entity's edges within clearance; None if it is unknown or above clearance."""
    rows = redact_above_clearance(rows, clearance_rank)
    # Above clearance reads as unknown so the entity's existence stays hidden.
    if not rows or _is_redacted(rows[0]["entity"]):
        return None
    found = [
        {"edge": row["edge"], "neighbour": row["neighbour"]}
        for row in rows
        if row["edge"] is not None
    ]
    visible = [e for e in found if not _is_redacted(e)]
    return {
        "entity": rows[0]["entity"],
        "edge_count": len(visible),
        "withheld": len(found) - len(visible),
        "edges": visible,
    }


def _fit(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """The payload as the model receives it: JSON values, trimmed to fit max_chars.

    Trailing rows/chunks/paths/edges are dropped (never cutting JSON) and counted
    in `omitted_for_size`; the caller's payload is not changed.
    """
    payload = {k: list(v) if isinstance(v, list) else v for k, v in payload.items()}
    text = json.dumps(payload, default=str, ensure_ascii=False)
    for key in ("rows", "chunks", "paths", "edges"):
        items = payload.get(key)
        while len(text) > max_chars and isinstance(items, list) and items:
            items.pop()
            payload["omitted_for_size"] = payload.get("omitted_for_size", 0) + 1
            text = json.dumps(payload, default=str, ensure_ascii=False)
    if len(text) > max_chars:
        raise ToolFailure(
            "result too large",
            {"error": "result too large; return fewer or smaller properties"},
            error_type="result_too_large",
        )
    return json.loads(text)


class ToolFailure(Exception):
    """An expected failure: the model gets payload, the audit detail, the span error_type."""

    def __init__(self, detail: str, payload: dict[str, Any], *, error_type: str):
        super().__init__(detail)
        self.detail = detail
        self.payload = payload
        self.error_type = error_type


@dataclass(frozen=True)
class _Call:
    """One tool call in progress: its investigation, tool name and start time."""

    ctx: InvestigationContext
    name: str
    max_chars: int
    started: float = field(default_factory=time.perf_counter)

    def finish(self, payload: dict[str, Any], counted: str) -> dict[str, Any]:
        """Record a successful call and return the payload as the model receives it.

        The audit detail counts the `counted` items actually sent. The payload says
        how many tool calls remain, so the model can plan to answer in time.
        """
        sent = _fit(
            {**payload, "tool_calls_left": self.ctx.calls_left()}, self.max_chars
        )
        detail = f"{len(sent[counted])} {counted}"
        self.ctx.complete_call(_summary(self.name, self.started, True, detail))
        return sent

    def reply(self, payload: dict[str, Any], counted: str) -> dict[str, Any]:
        """Record the chunk ids in a graph result as discovered, then finish."""
        found = collect_chunk_ids(payload)
        self.ctx.record_discovered(found)
        trace.get_current_span().set_attribute("nexo.graph.chunk_ids_found", len(found))
        return self.finish({**payload, "chunk_ids_found": sorted(found)}, counted)

    def fail(self, failure: ToolFailure) -> dict[str, Any]:
        """Record a failed call and return its (small, JSON-ready) error payload."""
        trace.get_current_span().set_attribute("error.type", failure.error_type)
        self.ctx.complete_call(_summary(self.name, self.started, False, failure.detail))
        return {**failure.payload, "tool_calls_left": self.ctx.calls_left()}


def _run_tool(
    ctx: InvestigationContext,
    name: str,
    max_chars: int,
    body: Callable[[_Call], dict[str, Any]],
) -> ToolResult:
    """Reserve a call from the budget, run body and return what the model receives.

    Failures are recorded as failed calls and returned with error status, so
    Strands marks the `execute_tool` span and its tool metrics as failed.
    """
    started = time.perf_counter()
    if not ctx.reserve_call():
        trace.get_current_span().set_attribute("error.type", "budget_exhausted")
        ctx.record_refused(
            _summary(name, started, False, "budget exhausted", refused=True)
        )
        return _result("error", _BUDGET_EXHAUSTED)
    call = _Call(ctx, name, max_chars, started)
    try:
        return _result("success", body(call))
    except ToolFailure as failure:
        return _result("error", call.fail(failure))
    # A bug, not an expected failure: neither the model nor the span gets its text.
    except Exception as exc:  # pylint: disable=broad-exception-caught
        log.exception("tool %s failed", name)
        failure = ToolFailure(
            "unexpected error", _UNEXPECTED, error_type=type(exc).__name__
        )
        return _result("error", call.fail(failure))


def _result(status: str, payload: dict[str, Any]) -> ToolResult:
    """A Strands tool result; the model sees payload as JSON text either way.

    A json block, not text: Strands copies an error's text into the span status,
    and a Cypher error can quote the query.
    """
    return {"status": status, "content": [{"json": payload}]}


def _read_graph(
    graph: GraphReader, cypher: str, parameters: dict[str, Any] | None = None
) -> ReadResult:
    """Run a read query; expected Neo4j failures raise ToolFailure."""
    try:
        return graph.run_read_query(cypher, parameters)
    except (ServiceUnavailable, SessionExpired) as exc:
        raise ToolFailure(
            "graph unavailable",
            {
                "error": "knowledge graph unavailable; do not retry, report what you have"
            },
            error_type="graph_unavailable",
        ) from exc
    except Neo4jError as exc:  # syntax errors, timeouts, access-mode violations
        raise ToolFailure(
            exc.code or "neo4j error",
            {"error": exc.message or str(exc), "code": exc.code},
            error_type=exc.code or "neo4j_error",
        ) from exc


def _chunk_store_failure(exc: Exception) -> ToolFailure:
    """Any chunk store failure (down, corrupt) goes back to the agent; the trace keeps it."""
    trace.get_current_span().record_exception(exc)
    name = type(exc).__name__
    return ToolFailure(name, _CHUNK_STORE_DOWN, error_type=name)


def build_investigation_tools(
    *,
    graph: GraphReader,
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
    redactor: ChunkRedactor | None = None,
) -> list[Any]:
    """Build the graph read, chunk fetch and chunk search tools for this investigation.

    Without a redactor, chunks above the caller's clearance are withheld whole.
    """
    redactor = redactor or ChunkRedactor(NoopDetector())
    return [
        _query_tool(graph, ctx, settings),
        *_lookup_tools(graph, ctx, settings),
        _fetch_tool(chunks, ctx, settings, redactor),
        _search_tool(chunks, ctx, settings, redactor),
    ]


def _query_tool(
    graph: GraphReader, ctx: InvestigationContext, settings: Settings
) -> Any:
    """The graph query tool."""
    clearance = ctx.scope.chunk_filter.max_sensitivity

    @tool
    def query_knowledge_graph(cypher_query: str) -> ToolResult:
        """Run one read-only Cypher query against the Neo4j knowledge graph.

        Use it for questions find_paths and entity_neighbours do not cover,
        such as looking entities up by name. Bound variable-length paths
        (for example -[*1..3]-) and always LIMIT results. Return the chunk_id or
        chunk_ids properties of the nodes and relationships you care about so
        their source text can be fetched. The result lists every chunk id it saw
        under "chunk_ids_found".

        Args:
            cypher_query: A single read-only Cypher statement (MATCH, OPTIONAL
                MATCH, WITH, WHERE, RETURN, UNWIND, CALL subqueries). Writes and
                procedure calls are rejected.
        """

        def run(call: _Call) -> dict[str, Any]:
            try:
                query = validate_read_only_cypher(cypher_query)
            except UnsafeCypherError as exc:
                raise ToolFailure(
                    str(exc),
                    {"error": str(exc), "hint": "rewrite as a single read-only query"},
                    error_type="unsafe_cypher",
                ) from exc
            result = _read_graph(graph, query)
            rows = redact_above_clearance(result.rows, clearance.rank)
            trace.get_current_span().set_attributes(
                {"nexo.graph.rows": len(rows), "nexo.graph.truncated": result.truncated}
            )
            payload: dict[str, Any] = {
                "row_count": len(rows),
                "truncated": result.truncated,
                "rows": rows,
            }
            if not rows:
                payload["hint"] = (
                    "no rows: try another name or alias, or search_chunks for the text"
                )
            return call.reply(payload, "rows")

        return _run_tool(
            ctx, "query_knowledge_graph", settings.tool_result_max_chars, run
        )

    return query_knowledge_graph


def _lookup_tools(
    graph: GraphReader, ctx: InvestigationContext, settings: Settings
) -> list[Any]:
    """The fixed path and neighbour lookups, so small models need no Cypher."""
    clearance = ctx.scope.chunk_filter.max_sensitivity
    max_chars = settings.tool_result_max_chars

    @tool
    def find_paths(
        from_key: str, to_key: str, max_hops: int = 3, max_paths: int = 5
    ) -> ToolResult:
        """Find the shortest chains of relationships connecting two entities.

        Hops follow relationships in either direction; each hop reports its
        real direction (from -> relation -> to) and its chunk_ids.

        Args:
            from_key: Entity key, e.g. "Organization:acme holdings".
            to_key: Entity key of the other end.
            max_hops: Longest path to consider, 1 to 4.
            max_paths: How many paths to return, shortest first, 1 to 10.
        """

        def run(call: _Call) -> dict[str, Any]:
            if from_key == to_key:
                raise ToolFailure(
                    "same entity",
                    {"error": "from_key and to_key are the same entity"},
                    error_type="same_entity",
                )
            hops, paths = min(max(max_hops, 1), 4), min(max(max_paths, 1), 10)
            result = _read_graph(
                graph,
                _PATHS.format(paths=paths, hops=hops, unranked=_UNKNOWN_RANK),
                {"from_key": from_key, "to_key": to_key},
            )
            if not result.rows:  # an endpoint key matched no entity
                raise ToolFailure("unknown key", _UNKNOWN_KEY, error_type="unknown_key")
            payload = visible_paths(result.rows, clearance.rank)
            if not payload["paths"]:
                where = (
                    "no visible path" if payload["withheld"] else "no path in the graph"
                )
                payload["hint"] = f"{where}: search_chunks for the text"
            return call.reply(payload, "paths")

        return _run_tool(ctx, "find_paths", max_chars, run)

    @tool
    def entity_neighbours(entity_key: str, limit: int = 25) -> ToolResult:
        """List one entity's direct relationships to other entities.

        Edges are in both directions ("outgoing" says which); RELATED_TO edges
        keep the original wording in "predicate". Best-supported edges first.

        Args:
            entity_key: Entity key, e.g. "Organization:acme holdings".
            limit: Most edges to return, 1 to 100.
        """

        def run(call: _Call) -> dict[str, Any]:
            result = _read_graph(
                graph,
                _NEIGHBOURS,
                {"key": entity_key, "limit": min(max(limit, 1), 100)},
            )
            payload = visible_neighbours(result.rows, clearance.rank)
            if payload is None:
                raise ToolFailure("unknown key", _UNKNOWN_KEY, error_type="unknown_key")
            if not payload["edges"]:
                payload["hint"] = "no edges in the graph: search_chunks for the text"
            return call.reply(payload, "edges")

        return _run_tool(ctx, "entity_neighbours", max_chars, run)

    return [find_paths, entity_neighbours]


def _fetch_tool(
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
    redactor: ChunkRedactor,
) -> Any:
    """The chunk fetch tool."""

    @tool
    def fetch_vector_chunks(chunk_ids: list[str]) -> ToolResult:
        """Fetch the exact source text for chunk ids found in the knowledge graph.

        This is the evidence: cite facts as [chunk:<chunk_id>] using only chunks
        returned here or given to you as seed passages. Chunks outside the
        caller's clearance or scope are withheld; personal details in a chunk
        above the caller's clearance appear as [EMAIL], [PHONE] and similar.

        Args:
            chunk_ids: Chunk ids (format chk_ followed by 16 hex characters) from graph results.
        """

        def run(call: _Call) -> dict[str, Any]:
            requested = list(dict.fromkeys(chunk_ids))
            well_formed = [c for c in requested if CHUNK_ID_RE.fullmatch(c)]
            valid = well_formed[: settings.agent_max_chunks_per_call]
            try:
                records = chunks.get_by_ids(valid)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise _chunk_store_failure(exc) from exc
            sensitivity = {r.chunk_id: r.sensitivity.value for r in records}
            views = [(r, redactor.visible(r, ctx.scope.chunk_filter)) for r in records]
            withheld = [r.chunk_id for r, shown in views if shown is None]
            returned = [_chunk_view(shown) for _, shown in views if shown]
            redacted = sum(shown.text != r.text for r, shown in views if shown)
            sent = call.finish(
                {
                    "chunks": returned,
                    "withheld": withheld,
                    "not_found": sorted(set(valid) - set(sensitivity)),
                    "invalid_ids": [c for c in requested if c not in well_formed],
                    "over_limit_not_fetched": well_formed[len(valid) :],
                },
                "chunks",
            )
            # Only text the model actually received counts as evidence.
            received = [c["chunk_id"] for c in sent["chunks"]]
            ctx.record_fetched({c: sensitivity[c] for c in received}, withheld)
            trace.get_current_span().set_attributes(
                {
                    "nexo.chunks.requested": len(valid),
                    "nexo.chunks.returned": len(received),
                    "nexo.chunks.withheld": len(withheld),
                    "nexo.chunks.redacted": redacted,
                }
            )
            return sent

        return _run_tool(
            ctx, "fetch_vector_chunks", settings.tool_result_max_chars, run
        )

    return fetch_vector_chunks


def _search_tool(
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
    redactor: ChunkRedactor,
) -> Any:
    """The chunk text search tool."""

    @tool
    def search_chunks(query: str, k: int = 5) -> ToolResult:
        """Search the source document text for passages that match a query.

        Use it when the graph has no matching entity or path: extraction can miss
        entities and relationships that the text states. Search with the names and
        key words from the question (hybrid keyword and semantic ranking). Results
        are evidence like fetched chunks: cite them as [chunk:<chunk_id>]. Only
        passages inside the caller's scope are searched; personal details in a
        passage above the caller's clearance appear as [EMAIL], [PHONE] and similar.

        Args:
            query: Entity names and key words, for example "Delta Devices".
            k: How many passages to return (at most the per-call chunk limit).
        """

        def run(call: _Call) -> dict[str, Any]:
            limit = max(1, min(k, settings.agent_max_chunks_per_call))
            try:
                allowed = redactor.search(chunks, query, limit, ctx.scope.chunk_filter)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                raise _chunk_store_failure(exc) from exc
            sent = call.finish(
                {"query": query, "chunks": [_chunk_view(r) for r in allowed]},
                "chunks",
            )
            sensitivity = {r.chunk_id: r.sensitivity.value for r in allowed}
            received = [c["chunk_id"] for c in sent["chunks"]]
            ctx.record_fetched({c: sensitivity[c] for c in received}, [])
            trace.get_current_span().set_attributes(
                {"nexo.chunks.requested": limit, "nexo.chunks.returned": len(received)}
            )
            return sent

        return _run_tool(ctx, "search_chunks", settings.tool_result_max_chars, run)

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


def _summary(
    name: str, started: float, ok: bool, detail: str, *, refused: bool = False
) -> ToolCallSummary:
    """The audit record of one tool call."""
    return ToolCallSummary(
        tool=name,
        ok=ok,
        refused=refused,
        detail=detail[:200],
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
