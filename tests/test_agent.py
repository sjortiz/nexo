"""Module B: tools in isolation, then the real Strands event loop against a
scripted OpenAI-compatible client (stands in for vLLM)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from neo4j.exceptions import Neo4jError, ServiceUnavailable
from openai.types.chat import ChatCompletion
from strands.models.openai import OpenAIModel
from strands.telemetry import tracer as strands_tracer

from nexo.agents.investigator import Investigator, extract_citations
from nexo.agents.models import InvestigationScope
from nexo.agents.tools import InvestigationContext, build_investigation_tools
from nexo.storage.schemas import Sensitivity
from nexo.storage.vector_store import ChunkFilter
from nexo.telemetry import apply_genai_env
from tests.conftest import FakeGraph, make_chunk


def _tools(settings, graph, chunk_store, *, clearance=Sensitivity.INTERNAL, budget=10):
    """Build the investigation context and its graph-query and chunk-fetch tools."""
    ctx = InvestigationContext(
        request_id="r1",
        max_tool_calls=budget,
        scope=InvestigationScope(chunk_filter=ChunkFilter(max_sensitivity=clearance)),
    )
    query, fetch = build_investigation_tools(
        graph=graph, chunks=chunk_store, ctx=ctx, settings=settings
    )
    return ctx, query, fetch


def test_query_tool_collects_chunk_ids_and_redacts_above_clearance(
    settings, chunk_store
):
    """Graph rows yield their chunk ids, and nodes above clearance are redacted with ids hidden."""
    graph = FakeGraph(
        rows=[
            {
                "a": {"name": "Acme", "chunk_ids": ["chk_" + "1" * 16]},
                "hop": {"chunk_id": "chk_" + "2" * 16},
            },
            {
                "a": {
                    "name": "Patient X",
                    "sensitivity_rank": 3,
                    "chunk_id": "chk_" + "3" * 16,
                }
            },
        ]
    )
    ctx, query, _ = _tools(settings, graph, chunk_store)
    out = json.loads(query("MATCH (a)-[r]-(b) RETURN a, r AS hop LIMIT 5"))
    assert out["chunk_ids_found"] == [
        "chk_" + "1" * 16,
        "chk_" + "2" * 16,
    ]  # redacted node's id not leaked
    assert out["rows"][1]["a"] == {"redacted": "above caller clearance"}
    assert ctx.calls[0].ok


def test_query_tool_rejects_writes_before_reaching_neo4j(settings, chunk_store):
    """Write Cypher is refused as a failed call without querying the graph."""
    graph = FakeGraph()
    ctx, query, _ = _tools(settings, graph, chunk_store)
    out = json.loads(query("MATCH (n) DETACH DELETE n"))
    assert "DETACH" in out["error"] and not graph.queries and not ctx.calls[0].ok


def test_query_tool_returns_neo4j_errors_as_feedback(settings, chunk_store):
    """Cypher errors come back with their Neo4j code and outages as an error, not raised."""
    # The driver has no public constructor for a server error carrying a code.
    syntax = Neo4jError._hydrate_neo4j(  # pylint: disable=protected-access
        code="Neo.ClientError.Statement.SyntaxError", message="Invalid input 'RETRN'"
    )
    _, query, _ = _tools(settings, FakeGraph(error=syntax), chunk_store)
    assert (
        json.loads(query("MATCH (n) RETRN n"))["code"]
        == "Neo.ClientError.Statement.SyntaxError"
    )
    _, query, _ = _tools(
        settings, FakeGraph(error=ServiceUnavailable("down")), chunk_store
    )
    assert "unavailable" in json.loads(query("MATCH (n) RETURN n"))["error"]


def test_fetch_tool_enforces_clearance_validates_ids_and_budget(settings, chunk_store):
    """Fetch withholds PHI, sorts out bad and unknown ids, records evidence, enforces budget."""
    public = make_chunk("d", 0, "Acme acquired Beta.")
    phi = make_chunk("d", 1, "Patient X diagnosed.", Sensitivity.PHI)
    chunk_store.upsert([public, phi], [public.text, phi.text])
    ctx, _, fetch = _tools(settings, FakeGraph(), chunk_store, budget=1)

    out = json.loads(
        fetch([public.chunk_id, phi.chunk_id, "not-an-id", "chk_" + "f" * 16])
    )
    assert [c["chunk_id"] for c in out["chunks"]] == [public.chunk_id]
    assert out["withheld"] == [phi.chunk_id]
    assert out["invalid_ids"] == ["not-an-id"] and out["not_found"] == [
        "chk_" + "f" * 16
    ]
    assert ctx.evidence.fetched == {
        public.chunk_id: "internal"
    }  # withheld text is never evidence
    assert "budget" in json.loads(fetch([public.chunk_id]))["error"]


def test_extract_citations():
    """Valid chunk citations are extracted in first-seen order, deduplicated, bogus ids dropped."""
    a, b = "chk_" + "a" * 16, "chk_" + "b" * 16
    assert extract_citations(f"X [chunk:{a}] y [chunk:{b}, {a}] z [chunk:bogus]") == [
        a,
        b,
    ]


# --- real Strands loop ------------------------------------------------------


def _completion(message: dict[str, Any], finish: str) -> ChatCompletion:
    """Build a chat completion with one assistant message and the given finish reason."""
    return ChatCompletion.model_validate(
        {
            "id": "c",
            "object": "chat.completion",
            "created": 0,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish,
                    "message": {"role": "assistant", **message},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> ChatCompletion:
    """Build a completion that calls one tool with JSON arguments."""
    return _completion(
        {
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        },
        "tool_calls",
    )


class ScriptedClient:  # pylint: disable=too-few-public-methods  # fakes one endpoint only
    """Mimics openai.AsyncOpenAI.chat.completions for a vLLM server."""

    def __init__(self, responses: list[ChatCompletion]) -> None:
        self._responses = iter(responses)
        self.requests: list[dict[str, Any]] = []
        self.chat = self
        self.completions = self

    async def create(self, **request: Any) -> ChatCompletion:
        """Record the request and return the next scripted completion."""
        self.requests.append(request)
        return next(self._responses)


def test_strands_spans_carry_conversation_and_tool_attributes(
    settings, chunk_store, spans
):
    """Agent and tool spans share the conversation id and carry graph and chunk counts."""
    evidence = make_chunk("deals", 0, "Acme Holdings acquired Beta Labs in 2024.")
    chunk_store.upsert([evidence], [evidence.text])
    client = ScriptedClient(
        [
            _tool_call(
                "t1",
                "query_knowledge_graph",
                {"cypher_query": "MATCH (n) RETURN n.chunk_id AS c LIMIT 1"},
            ),
            _tool_call("t2", "fetch_vector_chunks", {"chunk_ids": [evidence.chunk_id]}),
            _completion(
                {"content": f"Acme acquired Beta [chunk:{evidence.chunk_id}]."}, "stop"
            ),
        ]
    )
    Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(rows=[{"c": evidence.chunk_id}]),
        chunks=chunk_store,
        settings=settings,
    ).investigate(
        "q", request_id="thread-7", scope=InvestigationScope(strategy="graph")
    )
    finished = {s.name: s for s in spans.get_finished_spans()}
    agent = finished["invoke_agent relationship-investigator"]
    query_tool = finished["execute_tool query_knowledge_graph"]
    fetch_tool = finished["execute_tool fetch_vector_chunks"]
    assert agent.attributes["gen_ai.agent.name"] == "relationship-investigator"
    assert {
        s.attributes["gen_ai.conversation.id"] for s in (agent, query_tool, fetch_tool)
    } == {"thread-7"}
    assert (
        query_tool.attributes["nexo.graph.rows"] == 1
        and query_tool.attributes["nexo.graph.chunk_ids_found"] == 1
    )
    assert fetch_tool.attributes["nexo.chunks.returned"] == 1
    assert (
        finished["chunk_store get_by_ids"].parent.span_id == fetch_tool.context.span_id
    )


GENAI_ENV = (
    "OTEL_SEMCONV_STABILITY_OPT_IN",
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
)


@pytest.fixture
def clean_genai_env():
    """Clear the GenAI telemetry env vars before and after the test."""
    for name in GENAI_ENV:
        os.environ.pop(name, None)
    yield
    for name in GENAI_ENV:  # apply_genai_env writes os.environ directly
        os.environ.pop(name, None)


@pytest.fixture
def fresh_strands_tracer(monkeypatch):
    """Drop Strands' cached tracer so it rereads the GenAI env on first use."""
    monkeypatch.setattr(strands_tracer, "_tracer_instance", None)


@pytest.mark.usefixtures("clean_genai_env", "fresh_strands_tracer")
@pytest.mark.parametrize(
    ("capture", "content_exported"), [("no_content", False), ("span_only", True)]
)
def test_message_content_reaches_traces_only_when_capture_is_on(
    settings, chunk_store, spans, capture, content_exported
):
    """Prompt and tool content is exported in spans only when capture is enabled."""
    settings.otel_instrumentation_genai_capture_message_content = capture
    apply_genai_env(settings)

    evidence = make_chunk("d", 0, "Acme Holdings acquired Beta Labs.")
    chunk_store.upsert([evidence], [evidence.text])
    client = ScriptedClient(
        [
            _tool_call(
                "t1",
                "query_knowledge_graph",
                {"cypher_query": "MATCH (p) RETURN p.name AS name LIMIT 1"},
            ),
            _completion({"content": "Found it."}, "stop"),
        ]
    )
    Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(rows=[{"name": "Patient Jane Roe"}]),
        chunks=chunk_store,
        settings=settings,
    ).investigate("Who treated Jane Roe?", scope=InvestigationScope(strategy="graph"))

    exported = json.dumps(
        [
            [dict(s.attributes), [dict(e.attributes or {}) for e in s.events]]
            for s in spans.get_finished_spans()
        ],
        default=str,
    )
    assert ("Jane Roe" in exported) is content_exported


def test_strands_agent_crawls_graph_then_vectors(settings, chunk_store):
    """The real Strands loop queries the graph, fetches cited chunks and answers with citations."""
    evidence = make_chunk("deals", 0, "Acme Holdings acquired Beta Labs in 2024.")
    chunk_store.upsert([evidence], [evidence.text])
    graph = FakeGraph(
        rows=[
            {
                "a": "Acme Holdings",
                "rel": "ACQUIRED",
                "b": "Beta Labs",
                "chunk_id": evidence.chunk_id,
            }
        ]
    )
    client = ScriptedClient(
        [
            _tool_call(
                "t1",
                "query_knowledge_graph",
                {
                    "cypher_query": "MATCH (a:Entity)-[r]->(b:Entity) "
                    "RETURN a.name AS a, type(r) AS rel, "
                    "b.name AS b, r.chunk_id AS chunk_id LIMIT 10"
                },
            ),
            _tool_call("t2", "fetch_vector_chunks", {"chunk_ids": [evidence.chunk_id]}),
            _completion(
                {
                    "content": "Acme Holdings acquired Beta Labs in 2024 "
                    f"[chunk:{evidence.chunk_id}]."
                },
                "stop",
            ),
        ]
    )
    model = OpenAIModel(
        client=client, model_id="meta-llama/Llama-3.3-70B-Instruct", stream=False
    )
    investigator = Investigator(
        model=model, graph=graph, chunks=chunk_store, settings=settings
    )

    output = investigator.investigate(
        "How is Acme related to Beta Labs?", scope=InvestigationScope(strategy="graph")
    )

    assert output.error is None and output.stop_reason == "end_turn"
    assert output.citations == [evidence.chunk_id] == output.fetched_chunk_ids
    assert [c.tool for c in output.tool_calls] == [
        "query_knowledge_graph",
        "fetch_vector_chunks",
    ]
    assert len(graph.queries) == 1
    # Tool specs reached vLLM, and the graph result (with the chunk id) was fed back to the model.
    assert {t["function"]["name"] for t in client.requests[0]["tools"]} == {
        "query_knowledge_graph",
        "fetch_vector_chunks",
    }
    assert evidence.chunk_id in json.dumps(client.requests[1]["messages"])


def _seeded(chunk_store, replies):
    """Store a seed chunk; script `replies` with {id} replaced by its chunk id."""
    seed = make_chunk("kb", 0, "Beta Labs makes diagnostic kits.")
    chunk_store.upsert([seed], [seed.text])
    client = ScriptedClient(
        [_completion({"content": c.format(id=seed.chunk_id)}, "stop") for c in replies]
    )
    return seed, client


def _last_user_text(request) -> str:
    """Return the request's last user message as JSON text."""
    return json.dumps([m for m in request["messages"] if m["role"] == "user"][-1])


@pytest.mark.parametrize(
    ("first", "asks_for"),
    [
        ("", "without writing a final answer"),
        ("Beta Labs makes kits.", "has no citations"),
    ],
)
def test_missing_or_uncited_answer_gets_one_follow_up(
    settings, chunk_store, first, asks_for
):
    """An empty or uncited first answer triggers one follow-up asking for a cited answer."""
    seed, client = _seeded(chunk_store, [first, "Beta Labs makes kits [chunk:{id}]."])
    investigator = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    )
    output = investigator.investigate(
        "What does Beta Labs make?", scope=InvestigationScope(strategy="hybrid")
    )
    assert len(client.requests) == 2
    assert asks_for in _last_user_text(client.requests[1])
    assert output.citations == [seed.chunk_id] and output.answer.startswith(
        "Beta Labs makes kits"
    )


def test_cited_answer_and_answer_without_evidence_get_no_follow_up(
    settings, chunk_store
):
    """Cited answers, and answers when no evidence was fetched, are accepted as-is."""
    _, client = _seeded(chunk_store, ["Kits [chunk:{id}]."])
    Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    ).investigate("q", scope=InvestigationScope(strategy="hybrid"))
    assert len(client.requests) == 1

    nothing = ScriptedClient(
        [_completion({"content": "The knowledge graph has nothing on this."}, "stop")]
    )
    Investigator(
        model=OpenAIModel(client=nothing, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    ).investigate("q", scope=InvestigationScope(strategy="graph"))
    assert len(nothing.requests) == 1  # no evidence fetched: nothing to cite


def test_turn_capped_agent_is_asked_for_what_it_found(settings, chunk_store):
    """Hitting the turn cap without an answer prompts for a final answer from what was found."""
    client = ScriptedClient(
        [
            _tool_call(
                "t1",
                "query_knowledge_graph",
                {"cypher_query": "MATCH (n:Entity) RETURN n.name AS name LIMIT 5"},
            ),
            _completion(
                {"content": "The graph has no evidence about the chief executive."},
                "stop",
            ),
        ]
    )
    output = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(rows=[{"name": "Beta Labs"}]),
        chunks=chunk_store,
        settings=settings.model_copy(update={"agent_max_turns": 1}),
    ).investigate("Who runs Beta Labs?", scope=InvestigationScope(strategy="graph"))
    assert len(client.requests) == 2
    assert "without writing a final answer" in _last_user_text(client.requests[1])
    assert output.answer == "The graph has no evidence about the chief executive."


def test_follow_up_happens_once(settings, chunk_store):
    """A second empty answer is returned as-is rather than prompting again."""
    _, client = _seeded(chunk_store, ["", ""])
    output = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    ).investigate("q", scope=InvestigationScope(strategy="hybrid"))
    assert (
        len(client.requests) == 2 and output.answer == ""
    )  # compliance then rejects it


def test_investigate_works_inside_a_running_event_loop(settings, chunk_store):
    """investigate() works from inside a running event loop, as in a Jupyter cell."""
    seed = make_chunk("kb", 0, "Beta Labs makes diagnostic kits.")
    chunk_store.upsert([seed], [seed.text])
    client = ScriptedClient(
        [
            _completion(
                {"content": f"Beta Labs makes kits [chunk:{seed.chunk_id}]."}, "stop"
            )
        ]
    )
    investigator = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    )

    async def notebook_cell():
        return investigator.investigate(
            "What does Beta Labs make?", scope=InvestigationScope(strategy="hybrid")
        )

    output = asyncio.run(notebook_cell())
    assert output.error is None and output.citations == [seed.chunk_id]


def test_hybrid_strategy_seeds_vector_hits_into_prompt(settings, chunk_store):
    """The hybrid strategy puts vector-search hits into the first prompt as seed evidence."""
    seed = make_chunk("kb", 0, "Beta Labs makes diagnostic kits.")
    chunk_store.upsert([seed], [seed.text])
    client = ScriptedClient(
        [
            _completion(
                {"content": f"Beta Labs makes kits [chunk:{seed.chunk_id}]."}, "stop"
            )
        ]
    )
    investigator = Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=chunk_store,
        settings=settings,
    )
    output = investigator.investigate(
        "What does Beta Labs make?", scope=InvestigationScope(strategy="hybrid")
    )
    assert output.seed_chunk_ids == [seed.chunk_id] and output.citations == [
        seed.chunk_id
    ]
    assert "diagnostic kits" in json.dumps(client.requests[0]["messages"])
