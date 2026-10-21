"""The opt-in adapter that lets LangChain's Agent Inbox review Nexo's paused questions.

Agent Inbox talks to a LangGraph Server. Nexo runs no such server, so this serves the few routes
the inbox calls and turns each into a Nexo review call:

    POST /threads/search            -> Runtime.pending_reviews
    GET  /threads/{id}[/state]      -> the review packet as a HumanInterrupt
    POST /threads/{id}/runs[/stream]-> Runtime.review (accept, respond, ignore, edit)
    POST /threads/{id}/state        -> refused: only Runtime.review may release an answer

Every decision goes through Runtime.review, so the compliance node's rules apply unchanged.
The server binds 127.0.0.1 and answers only the inbox's own origin.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from pydantic import ValidationError

from nexo.orchestration.state import (
    ReviewDecision,
    ReviewPacket,
    ReviewPassage,
    describe_errors,
)
from nexo.service import RunResult

COMMENT_LIMIT = 2000
BODY_LIMIT = 1 << 20
ACCEPT_COMMENT = "approved in Agent Inbox"
IGNORE_COMMENT = "ignored in Agent Inbox"
REJECT_COMMENT = "rejected in Agent Inbox"
ACK_FIELD = "acknowledge_findings"
YES = {"yes", "true", "y"}


class ReviewBackend(Protocol):
    """The part of Runtime the adapter uses."""

    def pending_reviews(self, limit: int = 50) -> list[RunResult]:
        """Paused threads, newest first."""

    def review_passages(self, thread_id: str, clearance: str) -> list[ReviewPassage]:
        """Cited chunk text as a reader with this clearance sees it."""

    def review(self, thread_id: str, decision: ReviewDecision) -> RunResult:
        """Resume a paused thread."""

    def status(self, thread_id: str) -> RunResult:
        """A thread's current result."""


class InboxError(Exception):
    """A request the adapter refuses; `status` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _passages_markdown(passages: list[ReviewPassage], clearance: str) -> str:
    lines = [f"**Cited passages** (read at clearance `{clearance}`)"]
    for p in passages:
        head = f"`{p.chunk_id}`"
        if p.status == "shown":
            quoted = "\n".join(f"> {line}" for line in (p.text or "").splitlines())
            lines.append(f"{head} {p.source} ({p.classification})\n\n{quoted}")
        elif p.status == "withheld":
            lines.append(f"{head} withheld at your clearance (`{clearance}`)")
        else:
            lines.append(f"{head} not found")
    return "\n\n".join(lines)


def to_human_interrupt(
    packet: ReviewPacket, passages: list[ReviewPassage], clearance: str
) -> dict[str, Any]:
    """The Agent Inbox HumanInterrupt for one review packet.

    A packet without blocking findings can be accepted. One with blocking findings cannot:
    the inbox offers an edit form whose `acknowledge_findings` field must be set to yes, and
    the answer itself cannot be edited.
    """
    blocking = packet.blocking
    args: dict[str, Any] = {
        "request_id": packet.request_id,
        "question": packet.user_query,
        "draft_answer": packet.draft_answer,
    }
    if blocking:
        args[ACK_FIELD] = "no"
        args["comment"] = ""
    findings = [f"- **{f.severity}** `{f.code}`: {f.detail}" for f in packet.findings]
    description = "\n\n".join(
        [
            "**Findings**\n\n" + ("\n".join(findings) or "none"),
            _passages_markdown(passages, clearance),
        ]
        + (
            [f"Blocking findings: set `{ACK_FIELD}` to `yes` and submit to approve."]
            if blocking
            else []
        )
    )
    return {
        "action_request": {"action": "Review answer", "args": args},
        "config": {
            "allow_accept": not blocking,
            "allow_edit": blocking,
            "allow_respond": True,
            "allow_ignore": True,
        },
        "description": description,
    }


def to_decision(response: Any, packet: ReviewPacket, reviewer: str) -> ReviewDecision:
    """The ReviewDecision for one Agent Inbox response; raises InboxError when not allowed.

    Nothing here approves on its own: a blocked packet is approved only by an explicit
    acknowledgement, and ignore is a rejection.
    """
    try:
        return ReviewDecision(reviewer=reviewer, **_decision_fields(response, packet))
    except ValidationError as exc:
        raise InboxError(422, describe_errors(exc)) from exc


def _decision_fields(response: Any, packet: ReviewPacket) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise InboxError(422, "a response must be an object with type and args")
    kind, args = response.get("type"), response.get("args")
    if kind == "accept":
        if packet.blocking:
            raise InboxError(422, f"blocking findings: edit and set {ACK_FIELD} to yes")
        return {"approval_status": "approved", "comment": ACCEPT_COMMENT}
    if kind == "edit":
        return _edit_decision(args, packet)
    if kind == "response":
        if not isinstance(args, str):
            raise InboxError(422, "a response needs text")
        if len(args) > COMMENT_LIMIT:
            raise InboxError(422, f"a comment is at most {COMMENT_LIMIT} characters")
        return {
            "approval_status": "rejected",
            "comment": args.strip() or REJECT_COMMENT,
        }
    if kind == "ignore":
        return {"approval_status": "rejected", "comment": IGNORE_COMMENT}
    raise InboxError(422, f"unknown response type {kind!r}")


def _edit_decision(args: Any, packet: ReviewPacket) -> dict[str, Any]:
    """An edit approves only to acknowledge blocking findings; the answer stays as drafted."""
    edited = args.get("args") if isinstance(args, dict) else None
    if not isinstance(edited, dict):
        raise InboxError(422, "an edit needs the action's args")
    if not packet.blocking:
        raise InboxError(422, "this answer has no blocking findings: accept it")
    for key, drafted in (
        ("request_id", packet.request_id),
        ("question", packet.user_query),
        ("draft_answer", packet.draft_answer),
    ):
        if edited.get(key) != drafted:
            raise InboxError(422, f"{key} cannot be edited: Nexo has no edited answers")
    if str(edited.get(ACK_FIELD, "")).strip().lower() not in YES:
        raise InboxError(
            422, f"set {ACK_FIELD} to yes to approve, or respond to reject"
        )
    comment = str(edited.get("comment") or "").strip()
    if len(comment) > COMMENT_LIMIT:
        raise InboxError(422, f"a comment is at most {COMMENT_LIMIT} characters")
    return {
        "approval_status": "approved",
        "acknowledged_findings": True,
        "comment": comment or ACCEPT_COMMENT,
    }


def _timestamp(offset: int = 0) -> str:
    # Nexo keeps no thread times; newest-first order is kept by spacing them a second apart.
    when = datetime.now(timezone.utc) - timedelta(seconds=offset)
    return when.isoformat()


class InboxAdapter:
    """The routes' logic over a ReviewBackend, one request at a time."""

    def __init__(self, backend: ReviewBackend, *, reviewer: str, clearance: str):
        if not reviewer:
            raise ValueError("set NEXO_USER: reviews are recorded under your name")
        self.backend = backend
        self.reviewer = reviewer
        self.clearance = clearance
        self._lock = threading.Lock()

    def _interrupt(self, thread_id: str, packet: ReviewPacket) -> dict[str, Any]:
        try:
            passages = self.backend.review_passages(thread_id, self.clearance)
        except ValueError:  # no chunk store: show the answer without passages
            passages = []
        return to_human_interrupt(packet, passages, self.clearance)

    def _thread(self, result: RunResult, position: int = 0) -> dict[str, Any]:
        packet = result.pending_review
        when = _timestamp(position)
        thread: dict[str, Any] = {
            "thread_id": result.thread_id,
            "created_at": when,
            "updated_at": when,
            "metadata": {"graph_id": "nexo"},
            "status": "interrupted" if packet is not None else "idle",
            "values": {},
            "interrupts": {},
        }
        if packet is not None:
            value = self._interrupt(result.thread_id, packet)
            thread["interrupts"] = {result.thread_id: [{"value": value}]}
        return thread

    def info(self) -> dict[str, Any]:
        """GET /info."""
        return {"version": "nexo-review-inbox", "flags": {}}

    def search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """POST /threads/search: paused threads only."""
        limit = min(max(int(body.get("limit") or 10), 1), 100)
        offset = max(int(body.get("offset") or 0), 0)
        if body.get("status") not in (None, "interrupted"):
            return []
        with self._lock:
            found = self.backend.pending_reviews(limit=offset + limit)
            return [self._thread(r, i) for i, r in enumerate(found)][
                offset : offset + limit
            ]

    def get_thread(self, thread_id: str) -> dict[str, Any]:
        """GET /threads/{id}."""
        with self._lock:
            return self._thread(self.backend.status(thread_id))

    def get_state(self, thread_id: str) -> dict[str, Any]:
        """GET /threads/{id}/state."""
        with self._lock:
            result = self.backend.status(thread_id)
            tasks = []
            if result.pending_review is not None:
                value = self._interrupt(thread_id, result.pending_review)
                tasks = [
                    {
                        "id": thread_id,
                        "name": "compliance",
                        "interrupts": [{"value": value}],
                    }
                ]
            return {
                "values": {},
                "next": ["compliance"] if tasks else [],
                "tasks": tasks,
                "checkpoint": {"thread_id": thread_id},
                "metadata": {},
                "created_at": _timestamp(),
            }

    def decide(self, thread_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /threads/{id}/runs: the one way a decision reaches the graph."""
        resume = (body.get("command") or {}).get("resume")
        if body.get("input") is not None or not isinstance(resume, list):
            raise InboxError(422, "only a review decision can be sent to a thread")
        if len(resume) != 1:
            raise InboxError(422, "send exactly one response")
        with self._lock:
            packet = self.backend.status(thread_id).pending_review
            if packet is None:
                raise InboxError(409, f"thread {thread_id} is not awaiting review")
            decision = to_decision(resume[0], packet, self.reviewer)
            try:
                self.backend.review(thread_id, decision)
            except ValueError as exc:
                raise InboxError(409, str(exc)) from exc
        now = _timestamp()
        return {
            "run_id": uuid.uuid4().hex,
            "thread_id": thread_id,
            "assistant_id": body.get("assistant_id") or "nexo",
            "status": "success",
            "created_at": now,
            "updated_at": now,
            "metadata": {},
            "kwargs": {},
            "multitask_strategy": "reject",
        }

    def refuse_state_update(self) -> None:
        """POST /threads/{id}/state: Agent Inbox's raw ignore would end the thread unreviewed."""
        raise InboxError(
            403, "state updates are not accepted: decide with accept, respond or ignore"
        )


THREAD = re.compile(r"^/threads/([A-Za-z0-9_-]{1,100})(/state|/runs|/runs/stream)?$")


def allowed_origins(inbox_port: int) -> frozenset[str]:
    """The only origin the browser may call from: the inbox's own address."""
    return frozenset({f"http://127.0.0.1:{inbox_port}"})


class _Handler(BaseHTTPRequestHandler):
    """Routes for one adapter; make_handler binds adapter, origins and hosts."""

    server_version = "nexo-review-inbox"
    adapter: InboxAdapter
    origins: frozenset[str]
    hosts: frozenset[str]

    def log_message(self, *args: Any) -> None:  # quiet: threads hold question ids
        pass

    # -- plumbing ------------------------------------------------------
    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in self.origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, *, write: bool) -> bool:
        """Reject other hosts (DNS rebinding) and other origins (a web page's request)."""
        origin = self.headers.get("Origin")
        problem = None
        if self.headers.get("Host") not in self.hosts:
            problem = "unknown host"
        elif origin is not None and origin not in self.origins:
            problem = "origin not allowed"
        elif write and origin is None:
            problem = "origin required"
        elif write and not (self.headers.get("Content-Type") or "").startswith(
            "application/json"
        ):
            problem = "content type must be application/json"
        if problem:
            self._send(403, {"detail": problem})
        return problem is None

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > BODY_LIMIT:
            raise InboxError(413, "request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as exc:
            raise InboxError(400, "body is not JSON") from exc
        if not isinstance(body, dict):
            raise InboxError(400, "body must be an object")
        return body

    def _run(self, write: bool, handle) -> None:
        if not self._guard(write=write):
            return
        try:
            handle()
        except InboxError as exc:
            self._send(exc.status, {"detail": exc.message})
        except (TypeError, ValueError) as exc:
            self._send(400, {"detail": f"bad request: {exc}"})

    # -- routes --------------------------------------------------------
    def do_OPTIONS(self) -> None:  # pylint: disable=invalid-name
        """CORS preflight: answered only for the inbox's origin."""
        if self.headers.get("Origin") not in self.origins or (
            self.headers.get("Host") not in self.hosts
        ):
            self._send(403, {"detail": "origin not allowed"})
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "content-type, x-api-key, x-request-id"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        """GET /ok, /info, /threads/{id}, /threads/{id}/state."""

        def handle() -> None:
            path = self.path.split("?")[0]
            match = THREAD.match(path)
            if path == "/ok":
                self._send(200, {"ok": True})
            elif path == "/info":
                self._send(200, self.adapter.info())
            elif match and match.group(2) is None:
                self._send(200, self.adapter.get_thread(match.group(1)))
            elif match and match.group(2) == "/state":
                self._send(200, self.adapter.get_state(match.group(1)))
            else:
                raise InboxError(404, "not found")

        self._run(False, handle)

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """POST /threads/search, /threads/{id}/runs[/stream]; /state is refused."""

        def handle() -> None:
            path = self.path.split("?")[0]
            match = THREAD.match(path)
            if path == "/threads/search":
                self._send(200, self.adapter.search(self._body()))
            elif match and match.group(2) == "/state":
                self.adapter.refuse_state_update()
            elif match and match.group(2) == "/runs":
                self._send(200, self.adapter.decide(match.group(1), self._body()))
            elif match and match.group(2) == "/runs/stream":
                run = self.adapter.decide(match.group(1), self._body())
                self._stream(run)
            else:
                raise InboxError(404, "not found")

        self._run(True, handle)

    def _stream(self, run: dict[str, Any]) -> None:
        events = [
            ("metadata", {"run_id": run["run_id"]}),
            ("values", {}),
            ("end", None),
        ]
        text = "".join(
            f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
        )
        data = text.encode()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_handler(
    adapter: InboxAdapter, origins: frozenset[str], port: int
) -> type[BaseHTTPRequestHandler]:
    """The request handler class for one adapter, origin list and listening port."""
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    return type(
        "Handler",
        (_Handler,),
        {"adapter": adapter, "origins": origins, "hosts": hosts},
    )


def build_server(
    adapter: InboxAdapter, *, port: int, inbox_port: int
) -> ThreadingHTTPServer:
    """The adapter's HTTP server, bound to 127.0.0.1 and open to the inbox's origin only."""
    handler = make_handler(adapter, allowed_origins(inbox_port), port)
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
