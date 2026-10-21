"""A local web page for reviewing paused questions. For demos only.

No authentication: it binds to the loopback interface and refuses anything else.
Review over MCP is the supported default; see docs/operations/human-review.md.
"""

from __future__ import annotations

import getpass
import hmac
import html
import ipaddress
import re
import secrets
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError

from nexo.config import Settings
from nexo.orchestration.state import (
    ReviewDecision,
    ReviewPacket,
    ReviewPassage,
    describe_errors,
)
from nexo.service import RunResult

BANNER = "Demo: no authentication, local use only"
MAX_BODY = 64 * 1024
MAX_COMMENT = 2000
THREAD = re.compile(r"[A-Za-z0-9_-]{1,100}")
STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:60rem;margin:0 auto;padding:1rem}"
    ".banner{background:#fff3cd;border:1px solid #d4a72c;padding:.5rem 1rem;"
    "font-weight:600}pre{white-space:pre-wrap;background:#f4f4f4;padding:.75rem}"
    ".block{color:#b00020}.error{color:#b00020;font-weight:600}"
    "table{border-collapse:collapse;width:100%}td,th{text-align:left;padding:.4rem;"
    "border-bottom:1px solid #ddd}"
)

Page = tuple[int, str]


class ReviewRuntime(Protocol):
    """What the page needs from `nexo.service.Runtime`."""

    def pending_reviews(self, limit: int = 50) -> list[RunResult]:
        """Paused threads, newest first."""

    def review_passages(self, thread_id: str, clearance: str) -> list[ReviewPassage]:
        """Cited chunk text for the thread, through clearance and redaction."""

    def review(self, thread_id: str, decision: ReviewDecision) -> RunResult:
        """Resume a paused thread with a decision."""

    def status(self, thread_id: str) -> RunResult:
        """The thread's current result."""


def check_loopback(host: str) -> None:
    """Raise ValueError unless the host is localhost or a loopback address."""
    try:
        ok = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(
            f"the demo review page has no authentication and binds loopback only, not {host!r}"
        )


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _layout(title: str, reviewer: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset=utf-8><title>{_e(title)}</title>"
        f"<style>{STYLE}</style></head><body>"
        f'<div class="banner">{_e(BANNER)}</div>'
        f"<p>Reviewer: <strong>{_e(reviewer)}</strong> (from NEXO_USER)</p>"
        f"{body}</body></html>"
    )


def _paused_since(runtime: ReviewRuntime, thread_id: str) -> str:
    """The newest checkpoint of a paused thread is the pause itself."""
    try:
        saver = runtime.graph.checkpointer  # type: ignore[attr-defined]
        stamp = saver.get_tuple({"configurable": {"thread_id": thread_id}})
        then = datetime.fromisoformat(stamp.checkpoint["ts"])
    except (AttributeError, KeyError, TypeError, ValueError):
        return "unknown"
    seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _passage_item(passage: ReviewPassage) -> str:
    """One cited chunk. Withheld evidence is named, never shown as empty."""
    if passage.status == "shown" and passage.text is not None:
        shown = f"<pre>{_e(passage.text)}</pre>"
    elif passage.status == "withheld":
        shown = "<p><em>withheld at your clearance</em></p>"
    else:
        shown = f"<p><em>{_e(passage.status)}</em></p>"
    return (
        f"<li><strong>{_e(passage.chunk_id)}</strong> "
        f"{_e(passage.source)} ({_e(passage.classification)}){shown}</li>"
    )


class ReviewApp:  # pylint: disable=too-few-public-methods  # handle is the whole surface
    """Routes requests to the runtime; no sockets, so tests call `handle` directly."""

    def __init__(self, runtime: ReviewRuntime, settings: Settings) -> None:
        self.runtime = runtime
        self.clearance = settings.default_clearance
        self.reviewer = settings.nexo_user or getpass.getuser()
        self.csrf_token = secrets.token_urlsafe(32)

    def handle(self, method: str, target: str, body: bytes = b"") -> Page:
        """Return (status, html) for one request."""
        path = urlsplit(target).path
        if method == "GET" and path == "/":
            return 200, self._list()
        match = re.fullmatch(r"/review/([^/]+)", path)
        if match and THREAD.fullmatch(match.group(1)):
            if method == "GET":
                return self._detail(match.group(1))
            if method == "POST":
                return self._decide(match.group(1), body)
        return 404, self._page("Not found", "<p>Not found.</p>")

    def _page(self, title: str, body: str) -> str:
        return _layout(title, self.reviewer, body)

    def _list(self) -> str:
        rows = "".join(
            f'<tr><td><a href="/review/{_e(r.thread_id)}">'
            f"{_e(packet.user_query)}</a></td>"
            f"<td>{_e(_paused_since(self.runtime, r.thread_id))}</td>"
            f"<td>{len(packet.findings)}</td></tr>"
            for r in self.runtime.pending_reviews()
            if (packet := r.pending_review) is not None
        )
        table = (
            f"<table><tr><th>Question</th><th>Age</th><th>Findings</th></tr>{rows}</table>"
            if rows
            else "<p>No questions are waiting for review.</p>"
        )
        return self._page("Pending reviews", f"<h1>Pending reviews</h1>{table}")

    def _paused(self, thread_id: str) -> ReviewPacket | None:
        return self.runtime.status(thread_id).pending_review

    def _detail(self, thread_id: str, error: str = "", status: int = 200) -> Page:
        packet = self._paused(thread_id)
        if packet is None:
            return 404, self._page(
                "Not found", "<p>This thread is not waiting for review.</p>"
            )
        try:
            passages = self.runtime.review_passages(thread_id, self.clearance)
        except ValueError as exc:  # for example, the runtime has no chunk store
            passages = []
            cited = f"<li>No passage text: {_e(exc)}</li>"
        else:
            cited = "".join(_passage_item(p) for p in passages) or (
                "<li>No citations.</li>"
            )
        findings = (
            "".join(
                f'<li class="{_e(f.severity)}"><strong>{_e(f.severity)}'
                f"</strong> {_e(f.code)}: {_e(f.detail)}</li>"
                for f in packet.findings
            )
            or "<li>No findings.</li>"
        )
        ack = (
            '<p><label><input type="checkbox" name="acknowledge" value="1"> '
            "Acknowledge findings (required to approve)</label></p>"
            if packet.blocking
            else ""
        )
        notice = f'<p class="error">{_e(error)}</p>' if error else ""
        body = (
            f'<h1>Review</h1>{notice}<p><a href="/">Back to the list</a></p>'
            f"<h2>Question</h2><pre>{_e(packet.user_query)}</pre>"
            f"<h2>Draft answer</h2><pre>{_e(packet.draft_answer)}</pre>"
            f"<h2>Citations (clearance: {_e(self.clearance)})</h2><ul>{cited}</ul>"
            f"<h2>Compliance findings</h2><ul>{findings}</ul>"
            f'<form method="post" action="/review/{_e(thread_id)}">'
            f'<input type="hidden" name="csrf" value="{_e(self.csrf_token)}">'
            f"{ack}"
            '<p><label>Comment<br><textarea name="comment" rows="4" cols="60" '
            f'maxlength="{MAX_COMMENT}"></textarea></label></p>'
            '<button name="decision" value="approve">Approve</button> '
            '<button name="decision" value="reject">Reject</button></form>'
        )
        return status, self._page("Review", body)

    def _decide(self, thread_id: str, body: bytes) -> Page:
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
        if not hmac.compare_digest(form.get("csrf", ""), self.csrf_token):
            return 403, self._page("Forbidden", "<p>Missing or invalid CSRF token.</p>")
        decision = form.get("decision")
        if decision not in {"approve", "reject"}:
            return 400, self._page("Bad request", "<p>Choose approve or reject.</p>")
        paused = self._paused(thread_id)
        if paused is None:
            return self._detail(thread_id)  # a 404 page
        try:
            recorded = self._decision(paused, form)
        except ValueError as exc:
            return self._detail(thread_id, str(exc), 400)
        try:
            result = self.runtime.review(thread_id, recorded)
        except ValueError as exc:
            return 409, self._page("Not reviewed", f"<p>{_e(exc)}</p>")
        return 200, self._page(
            "Result",
            f"<h1>Result</h1><p>Thread {_e(result.thread_id)}: {_e(result.status)}, "
            f'{_e(result.approval_status)}.</p><p><a href="/">Back to the list</a></p>',
        )

    def _decision(self, paused: ReviewPacket, form: dict[str, str]) -> ReviewDecision:
        """The decision the form asks for; raises ValueError saying why it must not be sent."""
        comment = form.get("comment", "").strip()
        acknowledged = form.get("acknowledge") == "1"
        approve = form.get("decision") == "approve"
        if len(comment) > MAX_COMMENT:
            raise ValueError("The comment is too long.")
        if approve and paused.blocking and not acknowledged:
            raise ValueError("Blocking findings: acknowledge them to approve.")
        try:
            return ReviewDecision(
                approval_status="approved" if approve else "rejected",
                reviewer=self.reviewer,
                comment=comment or None,
                acknowledged_findings=acknowledged,
            )
        except ValidationError as exc:
            raise ValueError(describe_errors(exc)) from exc


def make_server(app: ReviewApp, host: str, port: int) -> HTTPServer:
    """Build the loopback-only server; raises ValueError for any other host."""
    check_loopback(host)

    class Handler(BaseHTTPRequestHandler):
        """Single-threaded: the checkpointer connection is used from one thread."""

        def _serve(self, body: bytes = b"") -> None:
            host_header = (self.headers.get("Host") or "").rsplit(":", 1)
            names = {"127.0.0.1", "localhost", "[::1]"}
            # A page on another origin or a rebound DNS name must not reach the app.
            origin = self.headers.get("Origin")
            if host_header[0] not in names or (
                origin and urlsplit(origin).netloc != self.headers.get("Host")
            ):
                status, page = 403, "Forbidden"
            else:
                status, page = app.handle(self.command, self.path, body)
            data = page.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # pylint: disable=invalid-name
            """Serve a page."""
            self._serve()

        def do_POST(self) -> None:  # pylint: disable=invalid-name
            """Take a decision."""
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.send_error(413)
                return
            self._serve(self.rfile.read(length))

    return HTTPServer((host, port), Handler)
