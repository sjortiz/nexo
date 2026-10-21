"""The investigator's tools, bound to one investigation's context.

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

    def calls_left(self) -> int:
        """Tool calls still available in the budget."""
        with self._lock:
            return max(self.max_tool_calls - len(self.calls), 0)

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


# Chunk text is served only by the chunk tools, through the redactor; graph
# rows are for structure. Neo4jChunkStore keeps these on the (:Chunk) nodes.
CHUNK_TEXT_KEYS = frozenset({"text", "search_text", "embedding"})
_UNKNOWN_RANK = Sensitivity.PHI.rank


def _rank(value: dict[str, Any]) -> int:
    """The map's sensitivity rank; a level that cannot be read counts as phi.

    Entities and edges carry `sensitivity_rank`, chunks and documents a
    `sensitivity` level. MENTIONED_IN maps (a verbatim `surface`) carry neither,
    so they count as phi.
    """
    rank = int(value.get("sensitivity_rank", 0) or 0)
    level = value.get("sensitivity")
    if level is not None:
        try:
            rank = max(rank, Sensitivity(level).rank)
        except ValueError:
            rank = _UNKNOWN_RANK
    if "surface" in value and "sensitivity_rank" not in value:
        rank = max(rank, _UNKNOWN_RANK)
    return rank


def redact_above_clearance(value: Any, clearance_rank: int) -> Any:
    """Redact node/relationship maps above the caller's clearance; drop chunk text.

    A map is redacted whole when its rank (see `_rank`) exceeds the clearance.
    Chunk text, search text and embeddings are dropped from every map whatever
    the clearance. Only catches values projected as maps; scalar projections
    (RETURN e.name) need database-side RBAC on the :PII/:PHI labels.
    """
    if isinstance(value, dict):
        if _rank(value) > clearance_rank:
            return {"redacted": "above caller clearance"}
        return {
            k: redact_above_clearance(v, clearance_rank)
            for k, v in value.items()
            if k not in CHUNK_TEXT_KEYS
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
           .certainty, .evidence, .chunk_ids,
           sensitivity_rank: coalesce(r.sensitivity_rank, 3)}}] AS hops"""

_NEIGHBOURS = """\
MATCH (e:Entity {key: $key})
OPTIONAL MATCH (e)-[r]-(n:Entity)
WITH e, r, n ORDER BY size(coalesce(r.chunk_ids, [])) DESC LIMIT $limit
RETURN e {.key, .name, .type, .sensitivity_rank} AS entity,
       r {relation: type(r), outgoing: startNode(r) = e, predicate: properties(r)['predicate'],
          valid_at: properties(r)['valid_at'], .certainty, .evidence, .chunk_ids,
          sensitivity_rank: coalesce(r.sensitivity_rank, 3)} AS edge,
       n {.key, .name, .type, .sensitivity_rank} AS neighbour"""

_UNKNOWN_KEY = {
    "error": "unknown entity key; look entities up by name and alias with "
    "query_knowledge_graph, or search_chunks for the text"
}

_BUDGET_EXHAUSTED = json.dumps(
    {"error": "tool budget exhausted; write your answer from the evidence gathered"}
)


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


def _dump(payload: dict[str, Any], max_chars: int) -> str:
    """Serialise, dropping trailing rows/chunks (never cutting JSON) to fit the budget."""
    text = json.dumps(payload, default=str, ensure_ascii=False)
    for key in ("rows", "chunks", "paths", "edges"):
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


@dataclass(frozen=True)
class _Call:
    """One tool call in progress: its investigation, tool name and start time."""

    ctx: InvestigationContext
    name: str
    started: float = field(default_factory=time.perf_counter)

    def fail(self, detail: str, payload: dict[str, Any]) -> str:
        """Record a failed call and return its error payload."""
        return _finish(
            self.ctx, _summary(self.name, self.started, False, detail), payload
        )

    def reply(self, payload: dict[str, Any], detail: str, max_chars: int) -> str:
        """Record the chunk ids in payload as discovered and return it to the model."""
        found: set[str] = set()
        collect_chunk_ids(payload, found)
        self.ctx.record_discovered(found)
        payload["chunk_ids_found"] = sorted(found)
        trace.get_current_span().set_attribute("nexo.graph.chunk_ids_found", len(found))
        summary = _summary(self.name, self.started, True, detail)
        return _finish(self.ctx, summary, payload, max_chars)


def _read_graph(
    graph: GraphReader,
    call: _Call,
    cypher: str,
    parameters: dict[str, Any] | None = None,
) -> ReadResult | str:
    """Run a read query; expected failures are recorded and returned as JSON."""
    # Strands' `execute_tool <name>` span is current here; the Neo4j CLIENT
    # span is a child created by GraphStore.
    span = trace.get_current_span()
    try:
        return graph.run_read_query(cypher, parameters)
    except (ServiceUnavailable, SessionExpired):
        span.set_attribute("error.type", "graph_unavailable")
        return call.fail(
            "graph unavailable",
            {
                "error": "knowledge graph unavailable; do not retry, report what you have"
            },
        )
    except Neo4jError as exc:  # syntax errors, timeouts, access-mode violations
        span.set_attribute("error.type", exc.code or "neo4j_error")
        return call.fail(
            exc.code or "neo4j error",
            {"error": exc.message or str(exc), "code": exc.code},
        )


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
    max_chars = settings.tool_result_max_chars

    @tool
    def query_knowledge_graph(cypher_query: str) -> str:
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
        if not ctx.reserve_call():
            return _BUDGET_EXHAUSTED
        call = _Call(ctx, "query_knowledge_graph")
        try:
            query = validate_read_only_cypher(cypher_query)
        except UnsafeCypherError as exc:
            trace.get_current_span().set_attribute("error.type", "unsafe_cypher")
            return call.fail(
                str(exc),
                {"error": str(exc), "hint": "rewrite as a single read-only query"},
            )
        result = _read_graph(graph, call, query)
        if isinstance(result, str):
            return result
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
        return call.reply(payload, f"{len(rows)} rows", max_chars)

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
    ) -> str:
        """Find the shortest chains of relationships connecting two entities.

        Hops follow relationships in either direction; each hop reports its
        real direction (from -> relation -> to) and its chunk_ids.

        Args:
            from_key: Entity key, e.g. "Organization:acme holdings".
            to_key: Entity key of the other end.
            max_hops: Longest path to consider, 1 to 4.
            max_paths: How many paths to return, shortest first, 1 to 10.
        """
        if not ctx.reserve_call():
            return _BUDGET_EXHAUSTED
        call = _Call(ctx, "find_paths")
        if from_key == to_key:
            return call.fail(
                "same entity", {"error": "from_key and to_key are the same entity"}
            )
        hops, paths = min(max(max_hops, 1), 4), min(max(max_paths, 1), 10)
        result = _read_graph(
            graph,
            call,
            _PATHS.format(paths=paths, hops=hops),
            {"from_key": from_key, "to_key": to_key},
        )
        if isinstance(result, str):
            return result
        if not result.rows:  # an endpoint key matched no entity
            return call.fail("unknown key", _UNKNOWN_KEY)
        payload = visible_paths(result.rows, clearance.rank)
        if not payload["paths"]:
            payload["hint"] = "no path in the graph: search_chunks for the text"
        return call.reply(payload, f"{payload['path_count']} paths", max_chars)

    @tool
    def entity_neighbours(entity_key: str, limit: int = 25) -> str:
        """List one entity's direct relationships to other entities.

        Edges are in both directions ("outgoing" says which); RELATED_TO edges
        keep the original wording in "predicate". Best-supported edges first.

        Args:
            entity_key: Entity key, e.g. "Organization:acme holdings".
            limit: Most edges to return, 1 to 100.
        """
        if not ctx.reserve_call():
            return _BUDGET_EXHAUSTED
        call = _Call(ctx, "entity_neighbours")
        result = _read_graph(
            graph,
            call,
            _NEIGHBOURS,
            {"key": entity_key, "limit": min(max(limit, 1), 100)},
        )
        if isinstance(result, str):
            return result
        payload = visible_neighbours(result.rows, clearance.rank)
        if payload is None:
            return call.fail("unknown key", _UNKNOWN_KEY)
        if not payload["edges"]:
            payload["hint"] = "no edges in the graph: search_chunks for the text"
        return call.reply(payload, f"{payload['edge_count']} edges", max_chars)

    return [find_paths, entity_neighbours]


def _fetch_tool(
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
    redactor: ChunkRedactor,
) -> Any:
    """The chunk fetch tool."""
    max_chars = settings.tool_result_max_chars

    @tool
    def fetch_vector_chunks(chunk_ids: list[str]) -> str:
        """Fetch the exact source text for chunk ids found in the knowledge graph.

        This is the evidence: cite facts as [chunk:<chunk_id>] using only chunks
        returned here or given to you as seed passages. Chunks outside the
        caller's clearance or scope are withheld; personal details in a chunk
        above the caller's clearance appear as [EMAIL], [PHONE] and similar.

        Args:
            chunk_ids: Chunk ids (format chk_ followed by 16 hex characters) from graph results.
        """
        if not ctx.reserve_call():
            return _BUDGET_EXHAUSTED
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
        sensitivity = {r.chunk_id: r.sensitivity.value for r in records}
        views = [(r, redactor.visible(r, ctx.scope.chunk_filter)) for r in records]
        withheld = [r.chunk_id for r, shown in views if shown is None]
        returned = [_chunk_view(shown) for _, shown in views if shown]
        redacted = sum(shown.text != r.text for r, shown in views if shown)
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
                "nexo.chunks.redacted": redacted,
            }
        )
        return text

    return fetch_vector_chunks


def _search_tool(
    chunks: ChunkReader,
    ctx: InvestigationContext,
    settings: Settings,
    redactor: ChunkRedactor,
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
        passages inside the caller's scope are searched; personal details in a
        passage above the caller's clearance appear as [EMAIL], [PHONE] and similar.

        Args:
            query: Entity names and key words, for example "Delta Devices".
            k: How many passages to return (at most the per-call chunk limit).
        """
        if not ctx.reserve_call():
            return _BUDGET_EXHAUSTED
        started = time.perf_counter()
        limit = max(1, min(k, settings.agent_max_chunks_per_call))
        # Strands' `execute_tool search_chunks` span is current here.
        span = trace.get_current_span()
        try:
            allowed = redactor.search(chunks, query, limit, ctx.scope.chunk_filter)
        # Any store failure (down, corrupt) goes back to the agent; the trace keeps it.
        except Exception as exc:  # pylint: disable=broad-exception-caught
            span.record_exception(exc)
            span.set_attribute("error.type", type(exc).__name__)
            return _finish(
                ctx,
                _summary("search_chunks", started, False, type(exc).__name__),
                {"error": "vector store unavailable; report what you have"},
            )
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
    """Record the call and return its payload as JSON for the model.

    The payload says how many tool calls remain, so the model can plan to answer
    instead of running into the limit.
    """
    ctx.complete_call(summary)
    payload["tool_calls_left"] = ctx.calls_left()
    return _dump(payload, max_chars)
