"""Storage failure spans keep query literals private unless content capture is enabled."""

from unittest.mock import MagicMock

import pytest
from neo4j.exceptions import Neo4jError, CypherSyntaxError
from opentelemetry.trace import StatusCode
from strands.models.openai import OpenAIModel

from nexo.agents.investigator import Investigator
from nexo.agents.models import InvestigationScope
from nexo.storage.graph_store import GraphStore
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore, _PROCEDURES
from nexo.telemetry import apply_genai_env, query_span
from tests.conftest import FakeGraph
from tests.test_agent import (  # pylint: disable=unused-import  # pytest fixtures
    ScriptedClient,
    _completion,
    _tool_call,
    clean_genai_env,
    fresh_strands_tracer,
)

# Imported fixtures also isolate Strands' process-wide content capture configuration.
pytestmark = pytest.mark.usefixtures("clean_genai_env", "fresh_strands_tracer")

SECRET = "JaneRoe-SSN-123"
CYPHER = f"MATCH (n:Person {{secret: '{SECRET}'}}) RETRN n"
CAPTURE = ["no_content", "span_only", "span_and_event"]


@pytest.fixture(name="failing_graph")
def fixture_failing_graph(settings, monkeypatch):
    """Use the real GraphStore with a driver that rejects the offending Cypher."""
    error = Neo4jError._hydrate_neo4j(  # pylint: disable=protected-access
        code="Neo.ClientError.Statement.SyntaxError",
        message=f"Invalid input 'RETRN':\n{CYPHER}",
    )
    driver, tx = MagicMock(), MagicMock()
    tx.run.side_effect = error
    driver.session.return_value.__enter__.return_value.execute_read.side_effect = (
        lambda work, *args, **kwargs: work(tx, *args, **kwargs)
    )
    monkeypatch.setattr(
        "nexo.storage.graph_store.GraphDatabase.driver", lambda *a, **kw: driver
    )
    return lambda: GraphStore.from_settings(settings, read_only=True)


@pytest.fixture(name="failing_chunks", params=["local", "neo4j"])
def fixture_failing_chunks(request, chunk_store, monkeypatch):
    """Fail inside an actual chunk storage span for each backend."""
    error = RuntimeError(f"chunk query failed: {SECRET}")

    def build(name):
        if request.param == "local":
            monkeypatch.setattr(
                chunk_store._store,  # pylint: disable=protected-access
                "get_by_ids" if name == "fetch_vector_chunks" else "similarity_search",
                MagicMock(side_effect=error),
            )
            return chunk_store
        graph = MagicMock()
        graph.session.return_value.__enter__.return_value.execute_read.side_effect = (
            error
        )
        store = Neo4jChunkStore(graph, MagicMock(), dimensions=16)
        # Skip version discovery so the failure occurs inside the actual query span.
        monkeypatch.setitem(store.__dict__, "_queries", _PROCEDURES)
        return store

    return build


def test_query_span_defaults_never_export_exception_content(spans):
    """The query wrapper suppresses SDK events and message-derived status by default."""
    with pytest.raises(ValueError, match=SECRET):
        with query_span("test query"):
            raise ValueError(SECRET)
    [span] = spans.get_finished_spans()
    assert span.attributes["error.type"] == "ValueError"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "storage operation failed"
    assert not span.events and SECRET not in span.to_json()


@pytest.mark.parametrize("capture", CAPTURE)
def test_cypher_syntax_error_content_is_opt_in(settings, failing_graph, spans, capture):
    """Real GraphStore failure handling protects every exported span field."""
    settings.otel_instrumentation_genai_capture_message_content = capture
    with failing_graph() as graph, pytest.raises(CypherSyntaxError):
        graph.run_read_query(CYPHER)

    [span] = spans.get_finished_spans()
    assert span.name == "agent_query"
    assert span.attributes["error.type"] == "CypherSyntaxError"
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "storage operation failed"
    assert (SECRET in span.to_json()) is settings.capture_content
    assert ("db.query.text" in span.attributes) is settings.capture_content
    assert bool(span.events) is settings.capture_content
    if settings.capture_content:
        [event] = span.events
        assert event.name == "exception"
        assert SECRET in event.attributes["exception.message"]


@pytest.mark.parametrize("capture", CAPTURE)
def test_cypher_failure_stays_private_through_the_agent(
    settings, failing_graph, chunk_store, spans, capture
):
    """The real Strands parent span stays safe while the model receives query feedback."""
    settings.otel_instrumentation_genai_capture_message_content = capture
    apply_genai_env(settings)
    client = ScriptedClient(
        [
            _tool_call("t1", "query_knowledge_graph", {"cypher_query": CYPHER}),
            _completion({"content": "Query failed."}, "stop"),
        ]
    )
    with failing_graph() as graph:
        Investigator(
            model=OpenAIModel(client=client, model_id="m", stream=False),
            graph=graph,
            chunks=chunk_store,
            settings=settings,
        ).investigate("Find a person.", scope=InvestigationScope(strategy="graph"))

    finished = spans.get_finished_spans()
    [tool] = [s for s in finished if s.name == "execute_tool query_knowledge_graph"]
    assert tool.attributes["error.type"] == "Neo.ClientError.Statement.SyntaxError"
    assert tool.status.description == "tool returned error status"
    assert (
        SECRET in "".join(s.to_json() for s in finished)
    ) is settings.capture_content
    assert SECRET in str(client.requests[-1]["messages"])


@pytest.mark.parametrize("capture", CAPTURE)
@pytest.mark.parametrize("name", ["fetch_vector_chunks", "search_chunks"])
def test_chunk_failure_content_is_opt_in(
    settings, failing_chunks, spans, capture, name
):
    """Chunk failures protect both storage children and real execute_tool parents."""
    settings.otel_instrumentation_genai_capture_message_content = capture
    apply_genai_env(settings)
    arguments = (
        {"chunk_ids": ["chk_" + "1" * 16]}
        if name == "fetch_vector_chunks"
        else {"query": "Acme"}
    )
    client = ScriptedClient(
        [
            _tool_call("t1", name, arguments),
            _completion({"content": "Store unavailable."}, "stop"),
        ]
    )
    Investigator(
        model=OpenAIModel(client=client, model_id="m", stream=False),
        graph=FakeGraph(),
        chunks=failing_chunks(name),
        settings=settings,
    ).investigate("Find a document.", scope=InvestigationScope(strategy="graph"))

    finished = spans.get_finished_spans()
    [tool] = [s for s in finished if s.name == f"execute_tool {name}"]
    assert tool.attributes["error.type"] == "RuntimeError"
    assert tool.status.status_code is StatusCode.ERROR
    assert tool.status.description == "tool returned error status"
    assert (
        SECRET in "".join(s.to_json() for s in finished)
    ) is settings.capture_content
    assert (
        bool([e for e in tool.events if e.name == "exception"])
        is settings.capture_content
    )
    if settings.capture_content:
        [event] = [e for e in tool.events if e.name == "exception"]
        assert SECRET in event.attributes["exception.message"]
    assert SECRET not in str(client.requests[-1]["messages"])
    [child] = [
        s for s in finished if s.parent and s.parent.span_id == tool.context.span_id
    ]
    assert child.attributes["error.type"] == "RuntimeError"
    assert child.status.status_code is StatusCode.ERROR
    assert child.status.description == "storage operation failed"
    assert not child.events
