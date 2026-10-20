"""The Agent Inbox adapter: packet mapping, decisions through Runtime.review, and who may call."""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import pytest

from nexo.review_inbox import (
    ACK_FIELD,
    IGNORE_COMMENT,
    InboxAdapter,
    InboxError,
    allowed_origins,
    build_server,
    to_decision,
    to_human_interrupt,
)
from nexo.service import RunResult
from tests.test_graph import FakeInvestigator, grounded_output, route, runtime

CHUNK = "chk_" + "a" * 16
ORIGIN = "http://127.0.0.1:3030"
SECRET = "the withheld sentence"


def packet(blocking: bool = False, **extra: Any) -> dict[str, Any]:
    """A review packet as the compliance node pauses with it."""
    findings = (
        [{"code": "pii_in_answer", "severity": "block", "detail": "an SSN"}]
        if blocking
        else []
    )
    return {
        "request_id": "t1",
        "user_query": "How is Acme related to Beta?",
        "draft_answer": f"Acme acquired Beta [chunk:{CHUNK}].",
        "citations": [CHUNK],
        "compliance_report": {"findings": findings, "blocking": blocking},
        **extra,
    }


def passages(status: str = "shown") -> list[dict[str, Any]]:
    """One cited passage, shown or withheld."""
    return [
        {
            "chunk_id": CHUNK,
            "source": "Memo > Deal",
            "text": "Acme bought Beta in 2024." if status == "shown" else None,
            "classification": "internal",
            "status": status,
        }
    ]


@dataclass
class FakeBackend:
    """Paused threads by id; records every review the adapter sends."""

    packets: dict[str, dict[str, Any]]
    shown: list[dict[str, Any]] = field(default_factory=passages)
    reviews: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _result(self, thread_id: str) -> RunResult:
        pending = self.packets.get(thread_id)
        return RunResult(
            thread_id=thread_id,
            status="awaiting_review" if pending else "completed",
            pending_review=pending,
        )

    def pending_reviews(self, limit: int = 50) -> list[RunResult]:
        """Paused threads in insertion order."""
        return [self._result(t) for t in list(self.packets)[:limit]]

    def review_passages(self, thread_id: str, clearance: str) -> list[dict[str, Any]]:
        """The configured passages."""
        assert thread_id in self.packets and clearance
        return self.shown

    def review(self, thread_id: str, decision: dict[str, Any]) -> RunResult:
        """Record the decision and unpause the thread."""
        if thread_id not in self.packets:
            raise ValueError(f"thread {thread_id} is not awaiting review")
        self.reviews.append((thread_id, decision))
        del self.packets[thread_id]
        return self._result(thread_id)

    def status(self, thread_id: str) -> RunResult:
        """The thread's current result."""
        return self._result(thread_id)


def adapter_for(backend: FakeBackend) -> InboxAdapter:
    """An adapter for a reviewer cleared for internal."""
    return InboxAdapter(backend, reviewer="rev@corp", clearance="internal")


def run(adapter: InboxAdapter, response: dict[str, Any], thread_id="t1"):
    """Send one Agent Inbox response the way the SDK does."""
    return adapter.decide(thread_id, {"command": {"resume": [response]}})


# -- the packet as a HumanInterrupt ---------------------------------------------------


def test_a_packet_becomes_an_acceptable_interrupt_with_its_passages():
    item = to_human_interrupt(packet(), passages(), "internal")
    assert item["action_request"]["args"]["draft_answer"].startswith("Acme acquired")
    assert item["config"] == {
        "allow_accept": True,
        "allow_edit": False,
        "allow_respond": True,
        "allow_ignore": True,
    }
    assert "Acme bought Beta in 2024." in item["description"]
    assert ACK_FIELD not in item["action_request"]["args"]


def test_blocking_findings_cannot_be_accepted_only_acknowledged():
    item = to_human_interrupt(packet(blocking=True), passages(), "internal")
    assert item["config"]["allow_accept"] is False
    assert item["config"]["allow_edit"] is True
    assert item["action_request"]["args"][ACK_FIELD] == "no"
    assert "pii_in_answer" in item["description"]


def test_a_withheld_passage_never_reaches_the_description():
    withheld = passages("withheld")
    withheld[0]["text"] = None
    item = to_human_interrupt(packet(), withheld, "internal")
    assert "withheld" in item["description"] and SECRET not in json.dumps(item)
    # the mapping prints text only for status shown, whatever the dict carries
    sneaky = passages("withheld")
    sneaky[0]["text"] = SECRET
    assert SECRET not in json.dumps(to_human_interrupt(packet(), sneaky, "internal"))


# -- every decision goes through Runtime.review ---------------------------------------


def test_accept_approves_as_the_configured_reviewer():
    backend = FakeBackend({"t1": packet()})
    run(adapter_for(backend), {"type": "accept", "args": None})
    ((thread_id, decision),) = backend.reviews
    assert thread_id == "t1"
    assert decision["approval_status"] == "approved"
    assert decision["reviewer"] == "rev@corp"
    assert "acknowledged_findings" not in decision


def test_accept_on_blocking_findings_is_refused_and_sends_nothing():
    backend = FakeBackend({"t1": packet(blocking=True)})
    with pytest.raises(InboxError) as err:
        run(adapter_for(backend), {"type": "accept", "args": None})
    assert err.value.status == 422 and not backend.reviews


def test_respond_rejects_with_the_text_as_comment():
    backend = FakeBackend({"t1": packet()})
    run(adapter_for(backend), {"type": "response", "args": "path is unverified"})
    assert backend.reviews[0][1]["approval_status"] == "rejected"
    assert backend.reviews[0][1]["comment"] == "path is unverified"


def test_a_response_is_never_an_approval_and_a_long_one_is_refused():
    backend = FakeBackend({"t1": packet()})
    adapter = adapter_for(backend)
    with pytest.raises(InboxError):
        run(adapter, {"type": "response", "args": "x" * 2001})
    with pytest.raises(InboxError):
        run(adapter, {"type": "response", "args": None})
    assert not backend.reviews


def test_ignore_is_an_explicit_rejection():
    backend = FakeBackend({"t1": packet()})
    run(adapter_for(backend), {"type": "ignore", "args": None})
    decision = backend.reviews[0][1]
    assert decision["approval_status"] == "rejected"
    assert decision["comment"] == IGNORE_COMMENT


def edit(adapter_packet: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """An inbox edit of a blocked packet: the args as shown, then the reviewer's changes."""
    args = to_human_interrupt(adapter_packet, [], "internal")["action_request"]["args"]
    return {"type": "edit", "args": {"action": "Review answer", "args": args | changes}}


def test_blocked_findings_are_approved_only_by_an_explicit_acknowledgement():
    blocked = packet(blocking=True)
    backend = FakeBackend({"t1": blocked})
    run(adapter_for(backend), edit(blocked, **{ACK_FIELD: "yes", "comment": "ok"}))
    decision = backend.reviews[0][1]
    assert decision["approval_status"] == "approved"
    assert decision["acknowledged_findings"] is True
    assert decision["comment"] == "ok"


@pytest.mark.parametrize(
    "changes",
    [
        {},  # the form as shown: acknowledgement still "no"
        {ACK_FIELD: "maybe"},
        {ACK_FIELD: "yes", "draft_answer": "A different answer"},  # no edited answers
        {ACK_FIELD: "yes", "request_id": "other"},
    ],
)
def test_an_edit_without_acknowledgement_or_with_a_changed_answer_is_refused(changes):
    blocked = packet(blocking=True)
    backend = FakeBackend({"t1": blocked})
    with pytest.raises(InboxError) as err:
        run(adapter_for(backend), edit(blocked, **changes))
    assert err.value.status == 422 and not backend.reviews


def test_an_edit_of_an_unblocked_answer_is_refused():
    clean = packet()
    with pytest.raises(InboxError):
        to_decision(edit(clean, **{ACK_FIELD: "yes"}), clean, "rev@corp")


def test_only_a_resume_command_is_accepted():
    backend = FakeBackend({"t1": packet()})
    adapter = adapter_for(backend)
    for body in (
        {"input": {"user_query": "new"}},
        {"command": {"resume": []}},
        {"command": {"resume": [{"type": "accept"}, {"type": "accept"}]}},
        {"command": {"goto": "END"}},
    ):
        with pytest.raises(InboxError):
            adapter.decide("t1", body)
    with pytest.raises(InboxError) as err:  # nothing paused under this id
        run(adapter, {"type": "ignore", "args": None}, thread_id="gone")
    assert err.value.status == 409 and not backend.reviews


def test_a_reviewer_is_required():
    with pytest.raises(ValueError):
        InboxAdapter(FakeBackend({}), reviewer="", clearance="internal")


# -- over HTTP: state updates, origins, binding ---------------------------------------


def free_port() -> int:
    """A port nothing listens on."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(name="serve")
def fixture_serve():
    """Start the adapter on a free port; yields (call, port) and stops it afterwards."""
    servers = []

    def start(backend):
        port = free_port()
        server = build_server(adapter_for(backend), port=port, inbox_port=3030)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return port

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def call(port, method, path, body=None, headers=None):
    """One request; returns (status, headers, parsed JSON or None)."""
    headers = {"Origin": ORIGIN, "Content-Type": "application/json"} | (headers or {})
    headers = {k: v for k, v in headers.items() if v is not None}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw, status, out = resp.read(), resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        raw, status, out = exc.read(), exc.code, exc.headers
    return status, out, json.loads(raw) if raw else None


def test_a_state_update_cannot_release_an_answer(serve, settings):
    """Agent Inbox's raw ignore posts as_node __end__: the thread must stay paused."""
    rt = runtime(settings, FakeInvestigator([grounded_output()]), [route()])
    rt.start("q", thread_id="t9")
    reviews = []

    class Backend:
        """The real runtime behind the adapter's interface, counting reviews."""

        def pending_reviews(self, limit=50):
            return [rt.status("t9")]

        def review_passages(self, thread_id, clearance):
            return []

        def review(self, thread_id, decision):
            reviews.append(decision)
            return rt.review(thread_id, decision)

        def status(self, thread_id):
            return rt.status(thread_id)

    port = serve(Backend())
    body = {"values": None, "as_node": "__end__"}
    for payload in (body, {"values": {"approval_status": "approved"}}):
        status, _, _ = call(port, "POST", "/threads/t9/state", payload)
        assert status == 403
    after = rt.status("t9")
    assert after.status == "awaiting_review" and after.final_response is None
    assert not reviews

    # the same thread is released only through a decision
    status, _, _ = call(
        port,
        "POST",
        "/threads/t9/runs",
        {"command": {"resume": [{"type": "accept", "args": None}]}},
    )
    done = rt.status("t9")
    assert status == 200 and done.approval_status == "approved" and len(reviews) == 1


def test_the_inbox_lists_and_reads_paused_threads(serve):
    port = serve(FakeBackend({"t1": packet(), "t2": packet(blocking=True)}))
    status, headers, listed = call(port, "POST", "/threads/search", {"limit": 10})
    assert status == 200 and [t["thread_id"] for t in listed] == ["t1", "t2"]
    assert listed[0]["status"] == "interrupted"
    assert listed[0]["interrupts"]["t1"][0]["value"]["config"]["allow_accept"] is True
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert call(port, "POST", "/threads/search", {"status": "idle"})[2] == []
    one = call(port, "GET", "/threads/t2/state")[2]
    assert one["tasks"][0]["interrupts"][0]["value"]["config"]["allow_edit"] is True


def test_only_the_inbox_origin_may_call_and_never_with_a_wildcard(serve):
    backend = FakeBackend({"t1": packet()})
    port = serve(backend)
    ok = call(port, "OPTIONS", "/threads/search")
    assert ok[0] == 204 and ok[1]["Access-Control-Allow-Origin"] == ORIGIN
    for origin in ("http://evil.example", "http://127.0.0.1:9999", "null"):
        for method, path in (("OPTIONS", "/threads/search"), ("GET", "/threads/t1")):
            status, headers, _ = call(port, method, path, headers={"Origin": origin})
            assert status == 403
            assert "Access-Control-Allow-Origin" not in headers
    assert allowed_origins(3030) == {ORIGIN}
    assert "*" not in "".join(allowed_origins(3030))


def test_a_web_page_cannot_post_a_decision_by_other_means(serve):
    backend = FakeBackend({"t1": packet()})
    port = serve(backend)
    accept = {"command": {"resume": [{"type": "accept", "args": None}]}}
    for headers in (
        {"Origin": "http://evil.example"},
        {"Origin": None},  # a script without an origin
        {"Content-Type": "text/plain"},  # a "simple" request with no preflight
        {"Host": "evil.example:80"},  # DNS rebinding
    ):
        assert call(port, "POST", "/threads/t1/runs", accept, headers)[0] == 403
    assert not backend.reviews


def test_the_server_binds_loopback_only(serve):
    port = serve(FakeBackend({}))
    server = build_server(adapter_for(FakeBackend({})), port=free_port(), inbox_port=1)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()
    assert call(port, "GET", "/ok")[0] == 200
