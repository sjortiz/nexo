"""The demo review page: list, decisions, escaping, CSRF and the loopback bind."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request

import pytest

from nexo.cli import main
from nexo.config import Settings
from nexo.orchestration.state import ReviewDecision, ReviewPassage
from nexo.review_ui import ReviewApp, make_server
from nexo.service import RunResult

THREAD = "t1"


def packet(question="Who owns A?", blocking=False, answer="B owns A."):
    """Test helper."""
    findings = [{"severity": "block", "code": "phi", "detail": "PHI in draft"}]
    return {
        "request_id": THREAD,
        "user_query": question,
        "draft_answer": answer,
        "citations": ["chk_1"],
        "compliance_report": {
            "findings": findings if blocking else [],
            "blocking": blocking,
        },
        "resume_with": {},
    }


class FakeRuntime:
    """Records decisions; holds paused and finished threads."""

    def __init__(self, *results, passages=None):
        self.results = {r.thread_id: r for r in results}
        self.passages = passages or [
            {
                "chunk_id": "chk_1",
                "source": "memo",
                "classification": "internal",
                "status": "shown",
                "text": "A is owned by B",
            }
        ]
        self.decisions = []

    def pending_reviews(self, limit=50):
        """Test helper."""
        return list(self.results.values())[:limit]

    def review_passages(self, _thread_id, _clearance):
        """Test helper."""
        return [ReviewPassage.model_validate(p) for p in self.passages]

    def status(self, thread_id):
        """Test helper."""
        return self.results.get(thread_id) or RunResult(
            thread_id=thread_id, status="completed"
        )

    def review(self, thread_id, decision):
        """Test helper."""
        self.decisions.append((thread_id, decision))
        return RunResult(
            thread_id=thread_id,
            status="completed",
            approval_status=decision.approval_status,
        )


def paused(thread_id=THREAD, **kwargs):
    """Test helper."""
    return RunResult(
        thread_id=thread_id,
        status="awaiting_review",
        pending_review=packet(**kwargs),
        paused_at=datetime.now(timezone.utc) - timedelta(hours=2, minutes=1),
    )


def make_app(*results, user="ana", **kwargs):
    """Test helper."""
    runtime = FakeRuntime(*results, **kwargs)
    return ReviewApp(runtime, Settings(nexo_user=user)), runtime


def post(app, fields, thread_id=THREAD, csrf=True):
    """Test helper."""
    data = (
        {"csrf": app.csrf_token}
        if csrf is True
        else ({} if csrf is None else {"csrf": csrf})
    )
    return app.handle(
        "POST",
        f"/review/{thread_id}",
        urllib.parse.urlencode({**data, **fields}).encode(),
    )


def test_list_shows_only_paused_threads():
    """List shows only paused threads."""
    done = RunResult(thread_id="t2", status="completed", final_response="x")
    app, _ = make_app(paused(question="open question"), done)
    status, page = app.handle("GET", "/")
    assert status == 200
    assert "open question" in page and "/review/t1" in page
    assert "/review/t2" not in page


def test_list_shows_age_and_findings_count():
    """List shows age and findings count."""
    app, _ = make_app(paused(blocking=True))
    page = app.handle("GET", "/")[1]
    assert "<td>2h</td><td>1</td>" in page


def test_withheld_passage_shows_no_text_and_masked_text_stays_masked():
    """Withheld passage shows no text and masked text stays masked."""
    withheld = {"chunk_id": "c1", "status": "withheld", "text": "SECRET-TEXT"}
    masked = {"chunk_id": "c2", "status": "shown", "text": "mail [EMAIL]"}
    app, _ = make_app(paused(), passages=[withheld, masked])
    page = app.handle("GET", "/review/t1")[1]
    assert "SECRET-TEXT" not in page and "withheld at your clearance" in page
    assert "mail [EMAIL]" in page


def test_passage_error_does_not_break_the_review_page():
    """Passage error does not break the review page."""
    app, runtime = make_app(paused())

    def no_store(*_args):
        raise ValueError("this runtime has no chunk store")

    runtime.review_passages = no_store
    status, page = app.handle("GET", "/review/t1")
    assert status == 200 and "no chunk store" in page and "Approve" in page


def test_empty_list_says_so():
    """Empty list says so."""
    app, _ = make_app()
    assert "No questions are waiting" in app.handle("GET", "/")[1]


def test_every_page_shows_banner_and_reviewer():
    """Every page shows banner and reviewer."""
    app, _ = make_app(paused())
    for target in ("/", "/review/t1", "/nowhere"):
        page = app.handle("GET", target)[1]
        assert "Demo: no authentication, local use only" in page
        assert "<strong>ana</strong>" in page


def test_review_page_shows_packet_and_passages():
    """Review page shows packet and passages."""
    app, _ = make_app(paused(blocking=True))
    _, page = app.handle("GET", "/review/t1")
    for text in ("Who owns A?", "B owns A.", "A is owned by B", "PHI in draft"):
        assert text in page
    assert "Acknowledge findings" in page


def test_review_page_for_a_finished_thread_is_not_found():
    """Review page for a finished thread is not found."""
    app, _ = make_app(paused())
    assert app.handle("GET", "/review/done")[0] == 404


@pytest.mark.parametrize(
    ("button", "expected"), [("approve", "approved"), ("reject", "rejected")]
)
def test_decision_calls_review_with_decision_and_reviewer(button, expected):
    """Decision calls review with decision and reviewer."""
    app, runtime = make_app(paused())
    status, page = post(app, {"decision": button, "comment": " looks right "})
    assert status == 200 and expected in page
    assert runtime.decisions == [
        (
            THREAD,
            ReviewDecision(
                approval_status=expected, reviewer="ana", comment="looks right"
            ),
        )
    ]


def test_reviewer_cannot_be_chosen_by_the_form():
    """Reviewer cannot be chosen by the form."""
    app, runtime = make_app(paused())
    post(app, {"decision": "approve", "reviewer": "mallory"})
    assert runtime.decisions[0][1].reviewer == "ana"


def test_blocked_report_cannot_be_approved_without_acknowledgement():
    """Blocked report cannot be approved without acknowledgement."""
    app, runtime = make_app(paused(blocking=True))
    status, page = post(app, {"decision": "approve"})
    assert status == 400 and "acknowledge" in page.lower()
    assert not runtime.decisions


def test_blocked_report_approves_when_acknowledged_and_rejects_freely():
    """Blocked report approves when acknowledged and rejects freely."""
    app, runtime = make_app(paused(blocking=True))
    assert post(app, {"decision": "approve", "acknowledge": "1"})[0] == 200
    assert runtime.decisions[0][1].acknowledged_findings is True
    assert post(app, {"decision": "reject"})[0] == 200
    assert runtime.decisions[1][1].approval_status == "rejected"


def test_decision_on_a_thread_not_paused_is_not_sent():
    """Decision on a thread not paused is not sent."""
    app, runtime = make_app(paused())
    assert post(app, {"decision": "approve"}, thread_id="gone")[0] == 404
    assert not runtime.decisions


def test_html_in_question_answer_passage_and_finding_is_escaped():
    """Html in question answer passage and finding is escaped."""
    attack = "<script>alert(1)</script>"
    evil = packet(question=attack, answer=attack, blocking=True)
    evil["compliance_report"]["findings"][0]["detail"] = attack
    item = RunResult(thread_id=THREAD, status="awaiting_review", pending_review=evil)
    passage = {"chunk_id": attack, "source": attack, "status": "shown", "text": attack}
    app, _ = make_app(item, passages=[passage])
    for target in ("/", "/review/t1"):
        page = app.handle("GET", target)[1]
        assert "<script>" not in page
        assert "&lt;script&gt;" in page


def test_html_in_a_thread_id_is_not_routed():
    """Html in a thread id is not routed."""
    app, _ = make_app(paused())
    status, page = app.handle("GET", "/review/<script>")
    assert status == 404 and "<script>" not in page


@pytest.mark.parametrize("token", [None, "", "wrong"])
def test_decision_without_a_valid_csrf_token_is_rejected(token):
    """Decision without a valid csrf token is rejected."""
    app, runtime = make_app(paused())
    status, _ = post(app, {"decision": "approve"}, csrf=token)
    assert status == 403
    assert not runtime.decisions


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "example.com", "::"])
def test_non_loopback_bind_is_refused(host):
    """Non loopback bind is refused."""
    app, _ = make_app()
    with pytest.raises(ValueError, match="loopback"):
        make_server(app, host, 0)


def test_cli_refuses_a_non_loopback_host(monkeypatch):
    """Cli refuses a non loopback host."""

    def not_opened(settings):
        raise AssertionError("the runtime must not open for a refused host")

    monkeypatch.setattr("nexo.cli.open_runtime", not_opened)
    assert main(["review-ui", "--host", "0.0.0.0"]) == 2


def test_served_over_http_on_loopback_and_foreign_host_header_refused():
    """Served over http on loopback and foreign host header refused."""
    app, _ = make_app(paused())
    server = make_server(app, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/") as response:
            assert b"Pending reviews" in response.read()
        rebound = urllib.request.Request(base + "/", headers={"Host": "evil.test"})
        with pytest.raises(urllib.error.HTTPError) as err:
            with urllib.request.urlopen(rebound):
                pass
        assert err.value.code == 403
    finally:
        server.shutdown()
        server.server_close()


def test_an_invalid_decision_is_shown_on_the_page_and_not_sent():
    """A decision ReviewDecision refuses (here, an overlong reviewer) is reported, not sent."""
    app, runtime = make_app(paused(), user="x" * 201)
    status, page = post(app, {"decision": "approve"})
    assert status == 400 and "reviewer: String should have at most 200" in page
    assert not runtime.decisions
