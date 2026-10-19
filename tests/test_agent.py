"""Module B: tools in isolation, then the real Strands event loop against a
scripted OpenAI-compatible client (stands in for vLLM)."""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from openai.types.chat import ChatCompletion
from strands.models.openai import OpenAIModel

from nexo.agents.investigator import Investigator, extract_citations
from nexo.agents.models import InvestigationScope
from nexo.agents.tools import InvestigationContext, build_investigation_tools
from nexo.storage.schemas import Sensitivity
from nexo.storage.vector_store import ChunkFilter
from nexo.telemetry import apply_genai_env
from tests.conftest import FakeGraph, make_chunk


def _tools(settings, graph, chunk_store, *, clearance=Sensitivity.INTERNAL, budget=10):
    ctx = InvestigationContext(request_id="r1", max_tool_calls=budget,
                               scope=InvestigationScope(chunk_filter=ChunkFilter(max_sensitivity=clearance)))
    query, fetch = build_investigation_tools(graph=graph, chunks=chunk_store, ctx=ctx, settings=settings)
    return ctx, query, fetch


def test_query_tool_collects_chunk_ids_and_redacts_above_clearance(settings, chunk_store):
    graph = FakeGraph(rows=[
        {"a": {"name": "Acme", "chunk_ids": ["chk_" + "1" * 16]}, "hop": {"chunk_id": "chk_" + "2" * 16}},
        {"a": {"name": "Patient X", "sensitivity_rank": 3, "chunk_id": "chk_" + "3" * 16}},
    ])
    ctx, query, _ = _tools(settings, graph, chunk_store)
    out = json.loads(query("MATCH (a)-[r]-(b) RETURN a, r AS hop LIMIT 5"))
    assert out["chunk_ids_found"] == ["chk_" + "1" * 16, "chk_" + "2" * 16]  # redacted node's id not leaked
    assert out["rows"][1]["a"] == {"redacted": "above caller clearance"}
    assert ctx.calls[0].ok


def test_query_tool_rejects_writes_before_reaching_neo4j(settings, chunk_store):
    graph = FakeGraph()
    ctx, query, _ = _tools(settings, graph, chunk_store)
    out = json.loads(query("MATCH (n) DETACH DELETE n"))
    assert "DETACH" in out["error"] and graph.queries == [] and not ctx.calls[0].ok


def test_query_tool_returns_neo4j_errors_as_feedback(settings, chunk_store):
    syntax = Neo4jError._hydrate_neo4j(code="Neo.ClientError.Statement.SyntaxError", message="Invalid input 'RETRN'")
    _, query, _ = _tools(settings, FakeGraph(error=syntax), chunk_store)
    assert json.loads(query("MATCH (n) RETRN n"))["code"] == "Neo.ClientError.Statement.SyntaxError"
    _, query, _ = _tools(settings, FakeGraph(error=ServiceUnavailable("down")), chunk_store)
    assert "unavailable" in json.loads(query("MATCH (n) RETURN n"))["error"]


def test_fetch_tool_enforces_clearance_validates_ids_and_budget(settings, chunk_store):
    public = make_chunk("d", 0, "Acme acquired Beta.")
    phi = make_chunk("d", 1, "Patient X diagnosed.", Sensitivity.PHI)
    chunk_store.upsert([public, phi], [public.text, phi.text])
    ctx, _, fetch = _tools(settings, FakeGraph(), chunk_store, budget=1)

    out = json.loads(fetch([public.chunk_id, phi.chunk_id, "not-an-id", "chk_" + "f" * 16]))
    assert [c["chunk_id"] for c in out["chunks"]] == [public.chunk_id]
    assert out["withheld"] == [phi.chunk_id]
    assert out["invalid_ids"] == ["not-an-id"] and out["not_found"] == ["chk_" + "f" * 16]
    assert ctx.fetched == {public.chunk_id: "internal"}  # withheld text is never evidence
    assert "budget" in json.loads(fetch([public.chunk_id]))["error"]


def test_extract_citations():
    a, b = "chk_" + "a" * 16, "chk_" + "b" * 16
    assert extract_citations(f"X [chunk:{a}] y [chunk:{b}, {a}] z [chunk:bogus]") == [a, b]


# --- real Strands loop ------------------------------------------------------


def _completion(message: dict[str, Any], finish: str) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "c", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    })


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> ChatCompletion:
    return _completion({"content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls")


class ScriptedClient:
    """Mimics openai.AsyncOpenAI.chat.completions for a vLLM server."""

    def __init__(self, responses: list[ChatCompletion]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **request: Any) -> ChatCompletion:
        self.requests.append(request)
        return next(self._responses)


def test_strands_spans_carry_conversation_and_tool_attributes(settings, chunk_store, spans):
    evidence = make_chunk("deals", 0, "Acme Holdings acquired Beta Labs in 2024.")
    chunk_store.upsert([evidence], [evidence.text])
    client = ScriptedClient([
        _tool_call("t1", "query_knowledge_graph", {"cypher_query": "MATCH (n) RETURN n.chunk_id AS c LIMIT 1"}),
        _tool_call("t2", "fetch_vector_chunks", {"chunk_ids": [evidence.chunk_id]}),
        _completion({"content": f"Acme acquired Beta [chunk:{evidence.chunk_id}]."}, "stop"),
    ])
    Investigator(model=OpenAIModel(client=client, model_id="m", stream=False),
                 graph=FakeGraph(rows=[{"c": evidence.chunk_id}]), chunks=chunk_store, settings=settings,
                 ).investigate("q", request_id="thread-7", scope=InvestigationScope(strategy="graph"))
    finished = {s.name: s for s in spans.get_finished_spans()}
    agent = finished["invoke_agent relationship-investigator"]
    query_tool = finished["execute_tool query_knowledge_graph"]
    fetch_tool = finished["execute_tool fetch_vector_chunks"]
    assert agent.attributes["gen_ai.agent.name"] == "relationship-investigator"
    assert {s.attributes["gen_ai.conversation.id"] for s in (agent, query_tool, fetch_tool)} == {"thread-7"}
    assert query_tool.attributes["nexo.graph.rows"] == 1 and query_tool.attributes["nexo.graph.chunk_ids_found"] == 1
    assert fetch_tool.attributes["nexo.chunks.returned"] == 1
    assert finished["chunk_store get_by_ids"].parent.span_id == fetch_tool.context.span_id


GENAI_ENV = ("OTEL_SEMCONV_STABILITY_OPT_IN", "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")


@pytest.fixture
def clean_genai_env():
    for name in GENAI_ENV:
        os.environ.pop(name, None)
    yield
    for name in GENAI_ENV:  # apply_genai_env writes os.environ directly
        os.environ.pop(name, None)


@pytest.mark.parametrize(("capture", "content_exported"), [("no_content", False), ("span_only", True)])
def test_message_content_reaches_traces_only_when_capture_is_on(clean_genai_env, settings, chunk_store, spans,
                                                                monkeypatch, capture, content_exported):
    import strands.telemetry.tracer as strands_tracer

    settings.otel_instrumentation_genai_capture_message_content = capture
    apply_genai_env(settings)
    monkeypatch.setattr(strands_tracer, "_tracer_instance", None)  # Strands reads the env on first use

    evidence = make_chunk("d", 0, "Acme Holdings acquired Beta Labs.")
    chunk_store.upsert([evidence], [evidence.text])
    client = ScriptedClient([
        _tool_call("t1", "query_knowledge_graph", {"cypher_query": "MATCH (p) RETURN p.name AS name LIMIT 1"}),
        _completion({"content": "Found it."}, "stop"),
    ])
    Investigator(model=OpenAIModel(client=client, model_id="m", stream=False),
                 graph=FakeGraph(rows=[{"name": "Patient Jane Roe"}]), chunks=chunk_store, settings=settings,
                 ).investigate("Who treated Jane Roe?", scope=InvestigationScope(strategy="graph"))

    exported = json.dumps([[dict(s.attributes), [dict(e.attributes or {}) for e in s.events]]
                           for s in spans.get_finished_spans()], default=str)
    assert ("Jane Roe" in exported) is content_exported


def test_strands_agent_crawls_graph_then_vectors(settings, chunk_store):
    evidence = make_chunk("deals", 0, "Acme Holdings acquired Beta Labs in 2024.")
    chunk_store.upsert([evidence], [evidence.text])
    graph = FakeGraph(rows=[{"a": "Acme Holdings", "rel": "ACQUIRED", "b": "Beta Labs", "chunk_id": evidence.chunk_id}])
    client = ScriptedClient([
        _tool_call("t1", "query_knowledge_graph",
                   {"cypher_query": "MATCH (a:Entity)-[r]->(b:Entity) RETURN a.name AS a, type(r) AS rel, "
                                    "b.name AS b, r.chunk_id AS chunk_id LIMIT 10"}),
        _tool_call("t2", "fetch_vector_chunks", {"chunk_ids": [evidence.chunk_id]}),
        _completion({"content": f"Acme Holdings acquired Beta Labs in 2024 [chunk:{evidence.chunk_id}]."}, "stop"),
    ])
    model = OpenAIModel(client=client, model_id="meta-llama/Llama-3.3-70B-Instruct", stream=False)
    investigator = Investigator(model=model, graph=graph, chunks=chunk_store, settings=settings)

    output = investigator.investigate("How is Acme related to Beta Labs?",
                                      scope=InvestigationScope(strategy="graph"))

    assert output.error is None and output.stop_reason == "end_turn"
    assert output.citations == [evidence.chunk_id] == output.fetched_chunk_ids
    assert [c.tool for c in output.tool_calls] == ["query_knowledge_graph", "fetch_vector_chunks"]
    assert len(graph.queries) == 1
    # Tool specs reached vLLM, and the graph result (with the chunk id) was fed back to the model.
    assert {t["function"]["name"] for t in client.requests[0]["tools"]} == {
        "query_knowledge_graph", "fetch_vector_chunks"}
    assert evidence.chunk_id in json.dumps(client.requests[1]["messages"])


def test_hybrid_strategy_seeds_vector_hits_into_prompt(settings, chunk_store):
    seed = make_chunk("kb", 0, "Beta Labs makes diagnostic kits.")
    chunk_store.upsert([seed], [seed.text])
    client = ScriptedClient([_completion({"content": f"Beta Labs makes kits [chunk:{seed.chunk_id}]."}, "stop")])
    investigator = Investigator(model=OpenAIModel(client=client, model_id="m", stream=False), graph=FakeGraph(),
                                chunks=chunk_store, settings=settings)
    output = investigator.investigate("What does Beta Labs make?", scope=InvestigationScope(strategy="hybrid"))
    assert output.seed_chunk_ids == [seed.chunk_id] and output.citations == [seed.chunk_id]
    assert "diagnostic kits" in json.dumps(client.requests[0]["messages"])
