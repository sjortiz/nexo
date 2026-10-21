"""CLI commands close what they open: the graph store and the review servers."""

from __future__ import annotations

import json
import logging
from http.server import HTTPServer

from nexo import cli
from nexo.agents.models import AgentOutput
from nexo.config import Settings


class _Graph:
    """A graph store that records whether it was closed."""

    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        """Record the close."""
        self.closed = True


class _Investigator:  # pylint: disable=too-few-public-methods
    """Cites one of the two chunks it fetched, and records the scope it was given."""

    def __init__(self) -> None:
        self.scopes: list = []

    def investigate(self, _question, *, scope) -> AgentOutput:
        """The same answer for every question."""
        self.scopes.append(scope)
        return AgentOutput(
            answer="Acme acquired Beta Labs.",
            citations=["chk_a"],
            fetched_chunk_ids=["chk_a", "chk_b"],
        )


def test_eval_scores_each_case_and_closes_the_graph(monkeypatch, tmp_path, capsys):
    """One JSON row per case, scored against its gold; the graph is closed after."""
    graph, investigator = _Graph(), _Investigator()
    settings = Settings(_env_file=None).model_copy(update={"judge_model": None})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_telemetry", lambda s: None)
    monkeypatch.setattr(
        cli.GraphStore, "from_settings", classmethod(lambda cls, s, read_only: graph)
    )
    monkeypatch.setattr(cli, "chunk_store_from_settings", lambda s, g: None)
    monkeypatch.setattr(cli, "build_investigator", lambda s, g, c: investigator)
    dataset = tmp_path / "golden.jsonl"
    cases = [
        {"question": "who?", "relevant_chunk_ids": ["chk_a"]},
        {"question": "what?", "answer_contains": ["beta"], "clearance": "phi"},
    ]
    dataset.write_text("\n\n".join(json.dumps(c) for c in cases), encoding="utf-8")

    try:
        assert cli.main(["eval", str(dataset)]) == 0
    finally:
        logging.getLogger().handlers.clear()  # main bound a handler to captured stderr

    first, second = (json.loads(r) for r in capsys.readouterr().out.splitlines())
    assert first["question"] == "who?"
    assert first["retrieval.context_precision"] == 0.5
    assert first["generation.citation_precision"] == 1.0
    assert second["generation.answer_contains"] == 1.0
    assert investigator.scopes[1].chunk_filter.max_sensitivity == "phi"
    assert graph.closed


class _InterruptedServer(HTTPServer):
    """A server stopped by Ctrl-C as soon as it starts serving."""

    def serve_forever(self, poll_interval=0.5):
        raise KeyboardInterrupt


def test_serve_stops_quietly_on_ctrl_c_and_closes_the_socket():
    """Ctrl-C ends serving without a traceback and releases the port."""
    server = _InterruptedServer(("127.0.0.1", 0), None)
    cli._serve(server)  # pylint: disable=protected-access
    assert server.socket.fileno() == -1


def test_clearance_choices_follow_sensitivity():
    """The CLI offers exactly the sensitivity levels, least sensitive first."""
    assert cli.CLEARANCES == ["public", "internal", "pii", "phi"]


class _FailingInvestigator(_Investigator):
    """Raises on the second question, like an agent hitting a hard limit."""

    def investigate(self, question, *, scope) -> AgentOutput:
        if len(self.scopes) == 1:
            self.scopes.append(scope)
            raise RuntimeError("agent exhausted")
        return super().investigate(question, scope=scope)


def test_eval_records_a_failed_case_and_scores_the_rest(monkeypatch, tmp_path, capsys):
    """One raising case gives an error row and a non-zero exit; the others are still scored."""
    graph, investigator = _Graph(), _FailingInvestigator()
    settings = Settings(_env_file=None).model_copy(update={"judge_model": None})
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "setup_telemetry", lambda s: None)
    monkeypatch.setattr(
        cli.GraphStore, "from_settings", classmethod(lambda cls, s, read_only: graph)
    )
    monkeypatch.setattr(cli, "chunk_store_from_settings", lambda s, g: None)
    monkeypatch.setattr(cli, "build_investigator", lambda s, g, c: investigator)
    dataset = tmp_path / "golden.jsonl"
    cases = [
        {"question": q, "relevant_chunk_ids": ["chk_a"]} for q in ("a?", "b?", "c?")
    ]
    dataset.write_text("\n".join(json.dumps(c) for c in cases), encoding="utf-8")

    try:
        assert cli.main(["eval", str(dataset)]) == 1
    finally:
        logging.getLogger().handlers.clear()

    rows = [json.loads(r) for r in capsys.readouterr().out.splitlines()]
    assert [r["question"] for r in rows] == ["a?", "b?", "c?"]
    assert rows[1] == {"question": "b?", "error": "RuntimeError"}
    assert (
        rows[0]["retrieval.context_precision"] == rows[2]["retrieval.context_precision"]
    )
    assert graph.closed
