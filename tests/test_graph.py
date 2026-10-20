"""Module C: the 3-node state machine, human-in-the-loop and tracing."""

from __future__ import annotations

import json
import logging
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.trace import SpanKind, StatusCode

from pydantic import SecretStr
from nexo.agents.models import AgentOutput, InvestigationScope
from nexo.cli import main
from nexo.orchestration.graph import (
    GraphDependencies,
    build_graph,
    workflow_mermaid,
    traced_node,
)
from nexo.service import RunResult, Runtime, _outcome, open_checkpointer
from nexo.storage.sensitive_data import RegexDetector
from nexo.telemetry import OtelLogHandler, SESSION_ID, get_tracer, query_attributes

CHUNK = "chk_" + "a" * 16


@dataclass
class FakeInvestigator:
    """Replays scripted outputs (raising exceptions) and records each call's query and scope."""

    outputs: list[AgentOutput | Exception]
    calls: list[tuple[str, InvestigationScope | None]] = field(
        default_factory=list, init=False
    )
    request_ids: list[str | None] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._outputs = list(self.outputs)

    def investigate(self, query, *, request_id=None, scope=None):
        """Record the call and return the next scripted output, repeating the last one."""
        self.calls.append((query, scope))
        self.request_ids.append(request_id)
        result = self._outputs.pop(0) if len(self._outputs) > 1 else self._outputs[0]
        if isinstance(result, Exception):
            raise result
        return result


def grounded_output(**overrides) -> AgentOutput:
    """Build an answer whose single citation was fetched, so compliance has no findings."""
    values = {
        "answer": f"Acme acquired Beta [chunk:{CHUNK}].",
        "citations": [CHUNK],
        "fetched_chunk_ids": [CHUNK],
        "fetched_sensitivity": {CHUNK: "internal"},
        "stop_reason": "end_turn",
    }
    return AgentOutput(**{**values, **overrides})


def route(target="investigator", **extra) -> AIMessage:
    """Build the router model's JSON reply choosing a target."""
    return AIMessage(content=json.dumps({"target": target, "reason": "test", **extra}))


def runtime(settings, investigator, router_replies=None, checkpointer=None) -> Runtime:
    """Compile the workflow around a fake investigator and optional scripted router replies."""
    router = (
        GenericFakeChatModel(messages=iter(router_replies))
        if router_replies is not None
        else None
    )
    deps = GraphDependencies(
        settings=settings,
        investigator=investigator,
        detector=RegexDetector(),
        router_llm=router,
    )
    return Runtime(
        graph=build_graph(deps, checkpointer or InMemorySaver()),
        settings=settings,
        detector=deps.detector,
    )


def _review(result: RunResult) -> dict[str, Any]:
    """Return the pending review payload, failing if the thread is not paused for review."""
    assert result.pending_review is not None, result
    # dict(): pylint reads the field's default (None), not its declared type.
    return dict(result.pending_review)


def _state(rt: Runtime, thread_id: str) -> dict[str, Any]:
    """Return the checkpointed workflow state of a thread."""
    return rt.graph.get_state({"configurable": {"thread_id": thread_id}}).values


def test_happy_path_pauses_for_review_then_releases_answer(settings):
    """A grounded answer pauses for review; approval releases it without re-running the agent."""
    investigator = FakeInvestigator([grounded_output()])
    rt = runtime(settings, investigator, [route(strategy="graph")])

    paused = rt.start("How is Acme related to Beta?", thread_id="t1")
    assert paused.status == "awaiting_review" and paused.final_response is None
    assert paused.approval_status == "pending"
    assert _review(paused)["draft_answer"].startswith("Acme acquired Beta")
    assert _review(paused)["compliance_report"]["blocking"] is False

    done = rt.review("t1", {"approval_status": "approved", "reviewer": "analyst@corp"})
    assert done.status == "completed" and done.approval_status == "approved"
    assert done.final_response == grounded_output().answer
    assert len(investigator.calls) == 1  # resume did not re-run the agent
    state = _state(rt, "t1")
    assert (
        len(state["agent_outputs"]) == 1
        and state["review"]["reviewer"] == "analyst@corp"
    )


def test_rejected_review_withholds_answer(settings):
    """A rejected review completes the thread without a final answer."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="t2")
    done = rt.review(
        "t2",
        {"approval_status": "rejected", "reviewer": "a", "comment": "insufficient"},
    )
    assert done.approval_status == "rejected" and done.final_response is None


def test_invalid_review_input_re_interrupts(settings):
    """An invalid decision re-pauses with an error and a valid one then completes."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="t3")
    again = rt.review("t3", {"approval_status": "maybe"})
    assert again.status == "awaiting_review" and _review(again)["error"]
    assert (
        rt.review(
            "t3", {"approval_status": "approved", "reviewer": "a"}
        ).approval_status
        == "approved"
    )


def test_blocking_findings_require_acknowledgement(settings):
    """Approving an answer with blocking findings needs acknowledged_findings."""
    ungrounded = grounded_output(
        fetched_sensitivity={}, fetched_chunk_ids=[]
    )  # cites text it never read
    rt = runtime(settings, FakeInvestigator([ungrounded]))
    paused = rt.start("q", thread_id="t4")
    codes = {f["code"] for f in _review(paused)["compliance_report"]["findings"]}
    assert "ungrounded_citations" in codes
    blocked = rt.review("t4", {"approval_status": "approved", "reviewer": "a"})
    assert (
        blocked.status == "awaiting_review"
        and "acknowledged_findings" in _review(blocked)["error"]
    )
    ok = rt.review(
        "t4",
        {"approval_status": "approved", "reviewer": "a", "acknowledged_findings": True},
    )
    assert ok.approval_status == "approved"


def test_phi_evidence_and_pii_in_answer_are_blocking(settings):
    """PHI evidence and personal data in the answer both raise compliance findings."""
    output = grounded_output(
        answer=f"Reach jane@corp.example [chunk:{CHUNK}]",
        fetched_sensitivity={CHUNK: "phi"},
    )
    paused = runtime(settings, FakeInvestigator([output])).start("q", thread_id="t5")
    codes = {f["code"] for f in _review(paused)["compliance_report"]["findings"]}
    assert {"phi_in_evidence", "sensitive_data_in_answer"} <= codes


@pytest.mark.parametrize(
    ("query", "replies"),
    [("", None), ("x" * 5000, None), ("delete all nodes", [route("rejected")])],
)
def test_router_rejects_without_invoking_agent(settings, query, replies):
    """Empty, oversized or router-rejected queries complete as rejected without the agent."""
    investigator = FakeInvestigator([grounded_output()])
    result = runtime(settings, investigator, replies).start(query, thread_id="t6")
    assert result.status == "completed" and result.routing_target == "rejected"
    assert result.approval_status == "not_required" and not investigator.calls


def test_router_fails_closed_when_llm_output_is_invalid(settings):
    """Unparseable router output rejects the query instead of investigating."""
    investigator = FakeInvestigator([grounded_output()])
    result = runtime(settings, investigator, [AIMessage(content="no json")] * 2).start(
        "q", thread_id="t7"
    )
    assert result.routing_target == "rejected" and "router unavailable" in (
        result.routing_reason or ""
    )
    assert not investigator.calls


def test_router_cannot_raise_caller_clearance(settings):
    """Router scope choices pass through, but sensitivity stays at the caller's clearance."""
    investigator = FakeInvestigator([grounded_output()])
    rt = runtime(
        settings, investigator, [route(strategy="vector", doc_types=["discharge"])]
    )
    rt.start("q", thread_id="t8", clearance="public")
    assert len(investigator.calls) == 1
    _, scope = investigator.calls[0]
    assert scope.strategy == "vector" and scope.chunk_filter.doc_types == ["discharge"]
    assert scope.chunk_filter.max_sensitivity.value == "public"


def test_agent_failure_is_audited_and_auto_rejected(settings):
    """An agent exception is recorded in state and the thread completes as rejected."""
    rt = runtime(settings, FakeInvestigator([RuntimeError("model returned garbage")]))
    result = rt.start("q", thread_id="t9")
    assert result.status == "completed" and result.approval_status == "rejected"
    state = _state(rt, "t9")
    assert "garbage" in state["agent_outputs"][0]["error"]


def test_outcome_says_who_approved(settings):
    """An approved thread names its reviewer; status stays the lifecycle state."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    assert rt.start("q", thread_id="o1").outcome == "awaiting review"
    done = rt.review("o1", {"approval_status": "approved", "reviewer": "analyst@corp"})
    assert done.status == "completed" and done.outcome == "approved by analyst@corp"
    assert rt.status("o1").outcome == "approved by analyst@corp"


def test_outcome_says_a_reviewer_rejected(settings):
    """A person's rejection names the reviewer; their comment stays in the result only."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="o2")
    done = rt.review(
        "o2", {"approval_status": "rejected", "reviewer": "a", "comment": "unverified"}
    )
    assert done.status == "completed" and done.outcome == "rejected by a"
    assert _state(rt, "o2")["review"]["comment"] == "unverified"


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_outcome_names_a_reviewer_called_system(settings, decision):
    """A person named `system` is a reviewer, not the compliance node's auto-reject."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="o6")
    done = rt.review("o6", {"approval_status": decision, "reviewer": "system"})
    assert done.approval_status == decision
    assert done.outcome == f"{decision} by system"


def test_outcome_shows_a_policy_approval_as_an_approval(settings):
    """The review policy's `system:auto` approval reads as approved, not system-rejected."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="o7")
    done = rt.review("o7", {"approval_status": "approved", "reviewer": "system:auto"})
    assert done.outcome == "approved by system:auto"


@pytest.mark.parametrize(
    "output", [AgentOutput(), RuntimeError("boom"), grounded_output(answer="")]
)
def test_outcome_shows_a_system_rejection(settings, output):
    """No releasable output completes as rejected, and the outcome says so."""
    rt = runtime(settings, FakeInvestigator([output]))
    result = rt.start("q", thread_id="o3")
    assert result.status == "completed" and result.approval_status == "rejected"
    assert result.outcome == "rejected: no releasable output"
    assert _state(rt, "o3")["review"]["source"] == "system"


@pytest.mark.parametrize(
    ("review", "outcome"),
    [
        (
            {"reviewer": "system", "comment": "no releasable output"},
            "rejected: no releasable output",
        ),
        (
            {"reviewer": "system", "comment": None, "acknowledged_findings": False},
            "rejected by system",
        ),
    ],
)
def test_outcome_reads_auto_rejects_checkpointed_before_the_marker(review, outcome):
    """A pre-marker auto-reject has no `source`; a reviewer always sets acknowledged_findings."""
    values = {"approval_status": "rejected", "review": review}
    assert _outcome(values, None) == outcome


def test_outcome_shows_a_router_rejection(settings):
    """A question the router refuses reads as rejected; its reason stays in the result."""
    reason = "asks for a patient's address"
    result = runtime(
        settings,
        FakeInvestigator([grounded_output()]),
        [route("rejected", reason=reason)],
    ).start("q", thread_id="o4")
    assert result.status == "completed" and result.approval_status == "not_required"
    assert result.outcome == "rejected: router refused"
    assert result.routing_reason == reason


def _cli(rt: Runtime, settings, *argv: str) -> None:
    """Run the CLI against an existing runtime."""
    with (
        patch("nexo.cli.get_settings", lambda: settings),
        patch("nexo.cli.open_runtime", lambda _settings: nullcontext(rt)),
    ):
        assert main(list(argv)) == 0


def test_cli_summary_shows_the_outcome_of_a_completed_thread(settings, capsys):
    """`nexo status` puts the outcome on the log line, so a rejection is not read as success."""
    rt = runtime(settings, FakeInvestigator([AgentOutput()]))
    rt.start("q", thread_id="o5")
    _cli(rt, settings, "status", "o5")
    assert "thread o5: completed (rejected: no releasable output)" in (
        capsys.readouterr().err
    )


def test_exported_summary_keeps_free_text_out_without_capture(settings):
    """With content capture off, the exported summary has no reviewer comment or router reason."""
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    otlp = OtelLogHandler(provider, capture_content=False)
    comment, reason = "SSN 123-45-6789 on page 2", "asks for a patient's address"
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.start("q", thread_id="o8")
    refused = runtime(
        settings,
        FakeInvestigator([grounded_output()]),
        [route("rejected", reason=reason)],
    )
    cli_log = logging.getLogger("nexo.cli")
    cli_log.addHandler(otlp)
    try:
        args = ["review", "o8", "--reject", "--reviewer", "a", "--comment", comment]
        _cli(rt, settings, *args)
        _cli(refused, settings, "ask", "q")
    finally:
        cli_log.removeHandler(otlp)
    bodies = [
        f"{log.log_record.body} {dict(log.log_record.attributes or {})}"
        for log in exporter.get_finished_logs()
    ]
    assert any("(rejected by a)" in body for body in bodies), bodies
    assert any("(rejected: router refused)" in body for body in bodies), bodies
    assert not any(comment in b or reason in b for b in bodies), bodies


def test_transient_vllm_error_retries_investigation(settings):
    """A vLLM connection error is retried and the second attempt reaches review."""
    transient = openai.APIConnectionError(
        request=httpx.Request("POST", "http://vllm-inference-service:8000/v1")
    )
    investigator = FakeInvestigator([transient, grounded_output()])
    result = runtime(settings, investigator).start("q", thread_id="t10")
    assert result.status == "awaiting_review" and len(investigator.calls) == 2


def test_review_of_unknown_thread_is_refused(settings):
    """Reviewing a thread that is not paused raises ValueError."""
    with pytest.raises(ValueError, match="not awaiting review"):
        runtime(settings, FakeInvestigator([grounded_output()])).review(
            "nope", {"approval_status": "approved"}
        )


def test_review_survives_process_restart_with_sqlite(settings):
    """A review paused in one SQLite checkpointer resumes from a fresh one without the agent."""
    settings.checkpoint_backend = "sqlite"
    with open_checkpointer(settings) as saver:
        runtime(
            settings, FakeInvestigator([grounded_output()]), checkpointer=saver
        ).start("q", thread_id="t11")
    investigator = FakeInvestigator([grounded_output()])
    with open_checkpointer(settings) as saver:  # new "process"
        done = runtime(settings, investigator, checkpointer=saver).review(
            "t11", {"approval_status": "approved", "reviewer": "a"}
        )
    assert done.approval_status == "approved" and not investigator.calls


def test_trace_follows_genai_conventions_across_the_handoff(settings, spans):
    """Workflow, node and agent spans nest and carry GenAI attributes and interrupt events."""

    @dataclass
    class SpanningInvestigator(FakeInvestigator):
        """Wraps each investigation in an agent span, as the Strands agent does."""

        def investigate(self, query, *, request_id=None, scope=None):
            """Investigate inside an invoke_agent span."""
            with get_tracer().start_as_current_span(
                "invoke_agent relationship-investigator"
            ):  # as Strands does
                return super().investigate(query, request_id=request_id, scope=scope)

    rt = runtime(settings, SpanningInvestigator([grounded_output()]))
    rt.start("q", thread_id="t12")
    finished = {s.name: s for s in spans.get_finished_spans()}
    workflow = finished["invoke_workflow nexo-investigation"]
    assert workflow.attributes["gen_ai.operation.name"] == "invoke_workflow"
    assert workflow.attributes["gen_ai.conversation.id"] == "t12"
    nodes = {
        n: finished[f"langgraph.node {n}"]
        for n in ("router", "investigator", "compliance")
    }
    assert all(
        span.parent.span_id == workflow.context.span_id for span in nodes.values()
    )
    # The agent span started inside the @task is a child of the investigator node.
    assert (
        finished["invoke_agent relationship-investigator"].parent.span_id
        == nodes["investigator"].context.span_id
    )
    assert nodes["router"].attributes["langgraph.state_update.keys"] == (
        "approval_status",
        "investigation_scope",
        "request_id",
        "routing_reason",
        "routing_target",
    )
    assert nodes["compliance"].attributes.get("langgraph.interrupted") is True
    assert any(e.name == "graph.interrupt" for e in workflow.events)


@pytest.mark.parametrize("doc", ["README.md", "docs/components/orchestration-graph.md"])
def test_published_workflow_diagram_matches_the_compiled_graph(doc):
    """Each published doc has exactly one mermaid block, equal to the compiled graph's."""
    text = (Path(__file__).resolve().parent.parent / doc).read_text()
    blocks = re.findall(r"```mermaid\n(.*?)\n```", text, re.DOTALL)
    assert blocks == [workflow_mermaid()], f"{doc}: regenerate with `nexo graph`"


def test_workflow_diagram_shows_every_node_and_the_rejection_branch(tmp_path):
    """Diagram has every node and the rejection edge, and `nexo graph` writes it to a file."""
    diagram = workflow_mermaid()
    for line in (
        "__start__ --> router;",
        "router -. &nbsp;rejected&nbsp; .-> __end__;",
        "router -.-> investigator;",
        "investigator --> compliance;",
        "compliance --> __end__;",
    ):
        assert line in diagram
    out = tmp_path / "workflow.mmd"
    assert main(["graph", "--output", str(out)]) == 0
    assert out.read_text().strip() == diagram


def test_openai_instrumentation_emits_one_chat_span_per_vllm_call(settings, spans):
    """One client chat span per vLLM call, under the router node, without message content."""

    def handler(_request: httpx.Request) -> httpx.Response:
        body = {
            "id": "c1",
            "object": "chat.completion",
            "created": 0,
            "model": "llama",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {"target": "investigator", "reason": "ok"}
                        ),
                    },
                }
            ],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }
        return httpx.Response(200, json=body)

    router = ChatOpenAI(
        base_url="http://vllm-inference-service:8000/v1",
        api_key="x",
        model="llama",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    deps = GraphDependencies(
        settings=settings,
        investigator=FakeInvestigator([grounded_output()]),
        detector=RegexDetector(),
        router_llm=router,
    )
    Runtime(graph=build_graph(deps, InMemorySaver()), settings=settings).start(
        "q", thread_id="t13"
    )

    chats = [s for s in spans.get_finished_spans() if s.name == "chat llama"]
    assert len(chats) == 1  # no duplicate inference spans
    chat = chats[0]
    router_node = next(
        s for s in spans.get_finished_spans() if s.name == "langgraph.node router"
    )
    assert (
        chat.kind is SpanKind.CLIENT
        and chat.parent.span_id == router_node.context.span_id
    )
    assert chat.attributes["gen_ai.operation.name"] == "chat"
    assert chat.attributes["server.address"] == "vllm-inference-service"
    assert chat.attributes["gen_ai.usage.input_tokens"] == 7
    assert not any(
        k.startswith("gen_ai.input.messages") for k in chat.attributes
    )  # content off by default


def test_the_request_span_says_who_asked_and_which_question(settings, spans):
    """user.id, session.id and the question's hash on the workflow span; never its text."""
    question = "How is Acme Holdings connected to Gamma Clinics?"
    # With content logging off: the gated content event is covered in test_content_logs.
    keyed = settings.model_copy(
        update={
            "query_hash_key": SecretStr("k1"),
            "telemetry_content_max_sensitivity": "none",
        }
    )
    rt = runtime(keyed, FakeInvestigator([grounded_output()]))
    started = rt.start(question, thread_id="t-id", user="ana")
    rt.review(started.thread_id, {"approval_status": "approved", "reviewer": "rev"})
    workflows = [
        s
        for s in spans.get_finished_spans()
        if s.name == "invoke_workflow nexo-investigation"
    ]
    start, review = workflows
    assert start.attributes["user.id"] == "ana"
    assert start.attributes["session.id"] == SESSION_ID
    assert (
        start.attributes["nexo.query.hash"]
        == query_attributes(keyed, question)["nexo.query.hash"]
    )
    assert review.attributes["user.id"] == "rev"  # the reviewer acts on the trace
    exported = " ".join(
        f"{dict(s.attributes)} {[dict(e.attributes or {}) for e in s.events]}"
        for s in spans.get_finished_spans()
    )
    assert question not in exported and "k1" not in exported


def test_a_review_pause_is_not_an_exception_on_the_trace(settings, spans):
    """The compliance pause carries the review packet: it must not become an exception event."""
    question = "Patient Jane Roe: who treated her?"
    runtime(settings, FakeInvestigator([grounded_output()])).start(question)
    compliance = next(
        s for s in spans.get_finished_spans() if s.name == "langgraph.node compliance"
    )
    assert compliance.attributes["langgraph.interrupted"] is True
    assert compliance.status.status_code != StatusCode.ERROR
    assert not [e for e in compliance.events if e.name == "exception"]
    assert question not in str([dict(e.attributes or {}) for e in compliance.events])


def test_a_failing_node_still_records_its_error(spans):
    """Real failures keep their exception event and ERROR status."""

    def broken(_state, _config):
        raise RuntimeError("model server down")

    node = traced_node("router", broken)
    with pytest.raises(RuntimeError):
        node({}, {"configurable": {"thread_id": "t"}})
    span = next(
        s for s in spans.get_finished_spans() if s.name == "langgraph.node router"
    )
    assert span.status.status_code == StatusCode.ERROR
    assert [e for e in span.events if e.name == "exception"]
