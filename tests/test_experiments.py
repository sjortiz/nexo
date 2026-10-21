"""`nexo eval` as a Langfuse experiment: the dataset, the item traces and the scores."""

from __future__ import annotations

import json
from contextlib import nullcontext
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nexo import cli
from nexo.agents.models import AgentOutput
from nexo.config import get_settings
from nexo.experiments import case_input, item_id
from nexo.telemetry import traced_span

CASES = [
    {
        "question": "Who acquired Beta Labs?",
        "relevant_chunk_ids": ["chk_a", "chk_b"],
        "reference_answer": "Acme Holdings.",
        "answer_contains": ["Acme"],
    },
    {"question": "Who supplies Gamma Clinics?", "clearance": "pii"},
]
EXPERIMENT = "langfuse.experiment"


class FakeLangfuse(ThreadingHTTPServer):
    """Langfuse's dataset, item and score endpoints on loopback; records every request."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[tuple[str, str, dict]] = []
        self.fail_scores = False

    @property
    def url(self) -> str:
        """The base URL, as LANGFUSE_HOST."""
        return f"http://127.0.0.1:{self.server_address[1]}"

    def bodies(self, path: str) -> list[dict]:
        """The JSON bodies posted to one path."""
        return [body for p, _auth, body in self.requests if p == path]


class _Handler(BaseHTTPRequestHandler):
    server: FakeLangfuse

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        """Answer like Langfuse 4.64 does."""
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        path = self.path.removeprefix("/api/public")
        self.server.requests.append((path, self.headers["Authorization"], body))
        if path == "/scores" and self.server.fail_scores:
            self.send_response(500)
            self.end_headers()
            return
        reply = {
            "/v2/datasets": {"id": "ds-1"},
            "/dataset-items": {"version": f"v-{body.get('id')}"},
            "/scores": {"id": body.get("id")},
        }[path]
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        """Quiet."""


@pytest.fixture(name="langfuse")
def fixture_langfuse():
    """A running fake Langfuse."""
    server = FakeLangfuse()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


class _Investigator:  # pylint: disable=too-few-public-methods
    """Answers every question citing chk_a, inside a child span like the agent's."""

    def __init__(self) -> None:
        self.questions: list[str] = []

    def investigate(self, question, scope) -> AgentOutput:
        """One child span, then a fixed answer."""
        del scope
        self.questions.append(question)
        with traced_span("invoke_agent"):
            return AgentOutput(
                answer="Acme Holdings acquired Beta Labs.",
                citations=["chk_a"],
                fetched_chunk_ids=["chk_a", "chk_c"],
            )


@pytest.fixture(name="run_eval")
def fixture_run_eval(monkeypatch, tmp_path):
    """Run `nexo eval` on CASES with a fake investigator; return (exit code, investigator)."""
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "EMBEDDING_PROVIDER": "fake",
        "JUDGE_MODEL": "",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    }.items():
        monkeypatch.setenv(key, value)
    investigator = _Investigator()
    monkeypatch.setattr(cli, "setup_telemetry", lambda _settings: None)
    monkeypatch.setattr(
        cli.GraphStore, "from_settings", lambda *_a, **_k: nullcontext()
    )
    monkeypatch.setattr(cli, "chunk_store_from_settings", lambda *_a: None)
    monkeypatch.setattr(cli, "build_investigator", lambda *_a: investigator)
    golden = tmp_path / "acme-gamma.jsonl"
    golden.write_text("".join(json.dumps(c) + "\n" for c in CASES))

    def run(*argv: str) -> tuple[int, _Investigator]:
        get_settings.cache_clear()
        try:
            return cli.main(["eval", str(golden), *argv]), investigator
        finally:
            get_settings.cache_clear()
            logging.getLogger().handlers.clear()

    return run


def _langfuse_env(monkeypatch, langfuse: FakeLangfuse) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", langfuse.url)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")


def test_the_golden_set_becomes_a_dataset_of_items(
    monkeypatch, langfuse, run_eval, spans
):
    """One dataset named after the file; each case an item with its input and expected output."""
    del spans
    _langfuse_env(monkeypatch, langfuse)
    code, _ = run_eval()
    assert code == 0
    assert langfuse.bodies("/v2/datasets") == [
        {"name": "acme-gamma", "description": "nexo eval golden set"}
    ]
    items = langfuse.bodies("/dataset-items")
    assert [item["input"]["question"] for item in items] == [
        c["question"] for c in CASES
    ]
    assert items[0]["expectedOutput"] == {
        "answer": "Acme Holdings.",
        "relevant_chunk_ids": ["chk_a", "chk_b"],
        "answer_contains": ["Acme"],
    }
    assert items[1]["input"] == {
        "question": "Who supplies Gamma Clinics?",
        "strategy": "hybrid",
        "clearance": "pii",
    }
    assert items[1]["expectedOutput"] == {}
    # The keys travel as Basic auth, never in a body.
    assert {auth for _p, auth, _b in langfuse.requests} == {
        "Basic cGstbGYtdGVzdDpzay1sZi10ZXN0"
    }
    assert "sk-lf-test" not in json.dumps([b for _p, _a, b in langfuse.requests])


def test_item_ids_are_stable_and_keyed_by_dataset_and_input(settings):
    """A rerun updates the same items; the same question at another clearance is another item."""
    inputs = case_input(CASES[0], settings)
    assert item_id("acme-gamma", inputs) == item_id("acme-gamma", dict(inputs))
    assert item_id("acme-gamma", inputs) != item_id("other", inputs)
    assert item_id("acme-gamma", inputs) != item_id(
        "acme-gamma", inputs | {"clearance": "phi"}
    )
    assert len(item_id("acme-gamma", inputs)) <= 255


def test_each_case_is_its_own_trace_rooted_at_its_item(
    monkeypatch, langfuse, run_eval, spans
):
    """eval.case has no parent and names its item; its children carry the run and item ids."""
    _langfuse_env(monkeypatch, langfuse)
    run_eval("--run-name", "baseline")
    finished = spans.get_finished_spans()
    roots = [s for s in finished if s.name == "eval.case"]
    children = [s for s in finished if s.name == "invoke_agent"]
    assert len(roots) == len(children) == 2
    assert len({s.context.trace_id for s in roots}) == 2
    items = langfuse.bodies("/dataset-items")
    for root, child, item in zip(roots, children, items):
        assert root.parent is None
        assert child.parent.span_id == root.context.span_id
        span_id = format(root.context.span_id, "016x")
        expected = {
            f"{EXPERIMENT}.name": "baseline",
            f"{EXPERIMENT}.dataset.id": "ds-1",
            f"{EXPERIMENT}.item.id": item["id"],
            f"{EXPERIMENT}.item.version": f"v-{item['id']}",
            f"{EXPERIMENT}.item.root_observation_id": span_id,
        }
        assert expected.items() <= dict(root.attributes).items()
        assert expected.items() <= dict(child.attributes).items()
        assert root.attributes[f"{EXPERIMENT}.metadata.agent_model"]
    run_ids = {s.attributes[f"{EXPERIMENT}.id"] for s in roots + children}
    assert len(run_ids) == 1


def test_each_metric_is_a_score_on_its_case_root(
    monkeypatch, langfuse, run_eval, spans
):
    """Non-null metrics only, numeric, on the case's trace and root span."""
    _langfuse_env(monkeypatch, langfuse)
    run_eval()
    roots = [s for s in spans.get_finished_spans() if s.name == "eval.case"]
    scores = langfuse.bodies("/scores")
    first = {
        s["name"]: s
        for s in scores
        if s["observationId"] == format(roots[0].context.span_id, "016x")
    }
    assert {name: s["value"] for name, s in first.items()} == {
        "retrieval.context_precision": 0.5,
        "retrieval.context_recall": 0.5,
        "generation.citation_precision": 1.0,
        "generation.answer_contains": 1.0,
    }
    for score in first.values():
        assert score["traceId"] == format(roots[0].context.trace_id, "032x")
        assert score["dataType"] == "NUMERIC"
    # The second case lists no relevant chunks: its recall is None and is not posted.
    second = [
        s["name"]
        for s in scores
        if s["observationId"] == format(roots[1].context.span_id, "016x")
    ]
    assert second == ["retrieval.context_precision", "generation.citation_precision"]
    assert len({s["id"] for s in scores}) == len(scores)


def test_a_score_that_fails_is_logged_and_the_run_goes_on(
    monkeypatch, langfuse, run_eval, capsys
):
    """Langfuse refusing a score does not lose the eval rows."""
    _langfuse_env(monkeypatch, langfuse)
    langfuse.fail_scores = True
    code, investigator = run_eval()
    assert code == 0
    assert len(investigator.questions) == 2
    captured = capsys.readouterr()
    assert len([json.loads(line) for line in captured.out.splitlines()]) == 2
    assert "not posted: langfuse /scores: HTTP 500" in captured.err


def test_an_unreachable_langfuse_stops_the_run_before_any_case(
    monkeypatch, langfuse, run_eval, capsys
):
    """The add-on is on but down: say so, run nothing."""
    _langfuse_env(monkeypatch, langfuse)
    monkeypatch.setenv("LANGFUSE_HOST", "http://127.0.0.1:9")
    code, investigator = run_eval()
    assert code == 1
    assert not investigator.questions
    err = capsys.readouterr().err
    assert "unreachable" in err and "make up LANGFUSE=1" in err


def test_missing_keys_stop_the_run(monkeypatch, langfuse, run_eval, capsys):
    """LANGFUSE_HOST without the project's keys is a setup error, not a silent skip."""
    monkeypatch.setenv("LANGFUSE_HOST", langfuse.url)
    code, investigator = run_eval()
    assert code == 1
    assert not investigator.questions
    assert not langfuse.requests
    assert "run make env" in capsys.readouterr().err


def test_without_the_add_on_nothing_goes_to_langfuse(langfuse, run_eval, spans):
    """No LANGFUSE_HOST: plain eval.case spans, no requests."""
    code, _ = run_eval()
    assert code == 0
    assert not langfuse.requests
    for span in spans.get_finished_spans():
        assert not any(key.startswith("langfuse.") for key in span.attributes)
