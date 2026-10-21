"""Failed tool calls: error results, span markers, audit records and compliance counts."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from strands.models.openai import OpenAIModel

from nexo.agents.investigator import Investigator
from nexo.agents.models import InvestigationScope
from nexo.agents.tools import InvestigationContext, build_investigation_tools
from nexo.orchestration.compliance import run_compliance_checks
from nexo.storage.graph_store import ReadResult
from nexo.storage.sensitive_data import NoopDetector
from tests.conftest import FakeGraph, make_chunk, model_text
from tests.test_agent import (
    ScriptedClient,
    _completion,
    _named_tools,
    _tool_call,
    _tools,
)


def test_fetch_tool_drops_trailing_chunks_to_fit_and_counts_only_those_sent(
    settings, chunk_store
):
    """Chunks dropped for size are counted in omitted_for_size and are not evidence."""
    first, second = (
        make_chunk("d", i, f"{'Acme acquired Beta. ' * 20}{i}") for i in (0, 1)
    )
    chunk_store.upsert([first, second], [first.text, second.text])
    small = settings.model_copy(update={"tool_result_max_chars": 800})
    ctx, _, fetch, _ = _tools(small, FakeGraph(), chunk_store)

    out = json.loads(fetch([first.chunk_id, second.chunk_id]))
    assert [c["chunk_id"] for c in out["chunks"]] == [first.chunk_id]
    assert out["omitted_for_size"] == 1
    assert ctx.evidence.fetched == {first.chunk_id: "internal"}
    assert ctx.completed_calls()[0].detail == "1 chunks"


def test_refused_calls_are_recorded_and_reserved_slots_hidden(settings, chunk_store):
    """A refusal is recorded as refused without taking a running call's slot."""
    ctx, tools = _named_tools(settings, FakeGraph(), chunk_store, budget=2)
    tools["entity_neighbours"]("X:a")
    assert ctx.reserve_call()  # a call still running
    tools["find_paths"]("X:a", "X:b")
    assert [(c.tool, c.ok, c.refused) for c in ctx.calls] == [
        ("entity_neighbours", False, False),
        ("(reserved)", False, False),
        ("find_paths", False, True),
    ]
    assert [c.tool for c in ctx.completed_calls()] == [
        "entity_neighbours",
        "find_paths",
    ]


@dataclass
class _ErrorOnTypo(FakeGraph):
    """Fails Cypher containing RETRN; lookups (with parameters) find nothing."""

    def run_read_query(self, cypher, parameters=None):
        if "RETRN" in cypher:
            raise Neo4jError._hydrate_neo4j(  # pylint: disable=protected-access
                code="Neo.ClientError.Statement.SyntaxError", message="Invalid input"
            )
        return ReadResult(rows=[] if parameters else [{"n": 1}], truncated=False)


def test_failed_tool_calls_are_failed_spans_and_match_compliance(
    settings, chunk_store, spans
):
    """Each failed or refused call marks its execute_tool span; the model reads the same JSON."""
    budget_of_three = settings.model_copy(update={"agent_max_tool_calls": 3})
    client = ScriptedClient(
        [
            _tool_call("t1", "find_paths", {"from_key": "X:a", "to_key": "X:b"}),
            _tool_call(
                "t2", "query_knowledge_graph", {"cypher_query": "MATCH (n) RETRN n"}
            ),
            _tool_call(
                "t3",
                "query_knowledge_graph",
                {"cypher_query": "MATCH (n) RETURN 1 AS n"},
            ),
            _tool_call("t4", "entity_neighbours", {"entity_key": "X:a"}),
            _completion({"content": "Nothing found."}, "stop"),
        ]
    )
    output = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=_ErrorOnTypo(),
        chunks=chunk_store,
        settings=budget_of_three,
    ).investigate("q", scope=InvestigationScope(strategy="graph"))

    tool_spans = [
        s for s in spans.get_finished_spans() if s.name.startswith("execute_tool ")
    ]
    failed = [s for s in tool_spans if s.status.status_code is StatusCode.ERROR]
    assert [s.attributes["error.type"] for s in failed] == [
        "unknown_key",
        "Neo.ClientError.Statement.SyntaxError",
        "budget_exhausted",
    ]
    assert all(s.attributes["gen_ai.tool.status"] == "error" for s in failed)
    assert {s.status.description for s in failed} == {"tool returned error status"}
    assert [(s.attributes["gen_ai.tool.name"], s in failed) for s in tool_spans] == [
        (c.tool, not c.ok) for c in output.tool_calls
    ]
    findings = {
        f.code: f.detail for f in run_compliance_checks(output, NoopDetector()).findings
    }
    assert findings["tool_errors"] == "2 of 3 tool calls failed"
    assert (
        findings["budget_exhausted"]
        == "1 tool call(s) refused after the budget was spent"
    )
    tool_errors, refusals = (
        int(findings[c].split()[0]) for c in ("tool_errors", "budget_exhausted")
    )
    assert len(failed) == tool_errors + refusals

    # The model reads the error as plain JSON text, as before the error status.
    unknown_key = next(
        m for m in client.requests[-1]["messages"] if m["role"] == "tool"
    )
    assert unknown_key["content"] == (
        '{"error": "unknown entity key; look entities up by name and alias with '
        'query_knowledge_graph, or search_chunks for the text", "tool_calls_left": '
        f"{json.loads(unknown_key['content'])['tool_calls_left']}}}"
    )


class _Broken:  # pylint: disable=too-few-public-methods  # fakes one method
    """A graph or chunk store whose every call raises exc."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise self.exc

        return fail


_SECRET = "Patient Jane Roe"
GRAPH_TOOLS = {"query_knowledge_graph", "find_paths", "entity_neighbours"}
_FAILURES = {
    "same_entity": (FakeGraph(), "find_paths", ("X:a", "X:a")),
    "unknown_key": (FakeGraph(), "entity_neighbours", ("X:a",)),
    "unsafe_cypher": (
        FakeGraph(),
        "query_knowledge_graph",
        ("MATCH (n) DETACH DELETE n",),
    ),
    "graph_unavailable": (
        FakeGraph(error=ServiceUnavailable("down")),
        "find_paths",
        ("X:a", "X:b"),
    ),
    "result_too_large": (
        FakeGraph(rows=[{"n": 1}]),
        "query_knowledge_graph",
        ("MATCH (n) RETURN n",),
    ),
    "ConnectionError": (
        _Broken(ConnectionError("down")),
        "fetch_vector_chunks",
        (["chk_" + "1" * 16],),
    ),
    "TimeoutError": (_Broken(TimeoutError("slow")), "search_chunks", ("Acme",)),
    "KeyError": (
        _Broken(KeyError(_SECRET)),
        "query_knowledge_graph",
        ("MATCH (n) RETURN n",),
    ),
}


@pytest.mark.parametrize("error_type", [*_FAILURES, "budget_exhausted"])
def test_every_failure_sets_error_type_and_records_a_failed_call(
    settings, chunk_store, spans, error_type
):
    """Each failure path returns error status, sets error.type and fails the call."""
    store, name, args = _FAILURES.get(
        error_type, (FakeGraph(), "find_paths", ("X:a", "X:b"))
    )
    small = settings.model_copy(update={"tool_result_max_chars": 60})
    ctx = InvestigationContext(
        request_id="r",
        max_tool_calls=0 if error_type == "budget_exhausted" else 5,
        scope=InvestigationScope(),
    )
    graph, chunks = (
        (store, chunk_store) if name in GRAPH_TOOLS else (FakeGraph(), store)
    )
    tools = {
        t.tool_name: t
        for t in build_investigation_tools(
            graph=graph, chunks=chunks, ctx=ctx, settings=small
        )
    }
    with trace.get_tracer("test").start_as_current_span("execute_tool"):
        result = tools[name](*args)

    [span] = spans.get_finished_spans()
    assert result["status"] == "error" and span.attributes["error.type"] == error_type
    [call] = ctx.completed_calls()
    assert not call.ok and call.refused is (error_type == "budget_exhausted")
    assert _SECRET not in model_text(result) + call.detail


def test_search_tool_counts_only_the_chunks_it_sent(settings, chunk_store):
    """Search results dropped for size are not evidence, and the audit counts what was sent."""
    first, second = (
        make_chunk("d", i, f"{'Acme acquired Beta. ' * 20}{i}") for i in (0, 1)
    )
    chunk_store.upsert([first, second], [first.text, second.text])
    small = settings.model_copy(update={"tool_result_max_chars": 800})
    ctx, _, _, search = _tools(small, FakeGraph(), chunk_store)

    out = json.loads(search("Acme acquired Beta", 2))
    assert len(out["chunks"]) == 1 and out["omitted_for_size"] == 1
    assert list(ctx.evidence.fetched) == [out["chunks"][0]["chunk_id"]]
    assert ctx.completed_calls()[0].detail == "1 chunks"
