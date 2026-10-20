"""CLI output: stdout carries only the command's JSON, logs go to stderr (issue #29)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from nexo import cli
from nexo.config import get_settings
from nexo.service import RunResult


@pytest.fixture(name="cli_env")
def fixture_cli_env(monkeypatch, tmp_path):
    """Run the CLI away from the repository's .env, with debug logs; yield monkeypatch."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()
    logging.getLogger().handlers.clear()  # the CLI bound a handler to the captured stderr


class _Runtime:
    """Stands in for the workflow runtime: every thread is awaiting review."""

    def start(self, query, **_kwargs) -> RunResult:
        """Start a thread; it pauses for review."""
        logging.getLogger("nexo.service").info("investigating %s", query)
        return RunResult(thread_id="t1", status="awaiting_review")

    def status(self, thread_id) -> RunResult:
        """Where the thread stands."""
        return RunResult(thread_id=thread_id, status="awaiting_review")


@contextmanager
def _open_runtime(_settings):
    yield _Runtime()


class _ChunkStore:  # pylint: disable=too-few-public-methods
    """A Neo4j chunk store with three chunks lacking text."""

    def missing_text(self) -> int:
        """Graph chunks without text."""
        return 3


class _Graph:  # pylint: disable=too-few-public-methods
    def close(self) -> None:
        """Nothing to close."""


@pytest.mark.parametrize(
    ("argv", "code", "expected"),
    [
        (["ask", "who acquired Beta Labs?"], 0, {"thread_id": "t1"}),
        (["status", "t1"], 0, {"thread_id": "t1", "status": "awaiting_review"}),
        (["chunks", "status"], 1, {"chunks_without_text": 3}),
    ],
)
def test_stdout_is_the_commands_json_and_logs_go_to_stderr(
    cli_env, capsys, argv, code, expected
):
    """`nexo ask | jq` works: stdout parses as one JSON document; the log lines are on stderr."""
    cli_env.setattr("nexo.cli.open_runtime", _open_runtime)
    cli_env.setattr(
        "nexo.cli._chunk_store", lambda _settings: (_Graph(), _ChunkStore())
    )
    assert cli.main(argv) == code
    captured = capsys.readouterr()
    result = json.loads(captured.out)  # fails on any log line or stray print
    assert expected.items() <= result.items()
    assert "nexo.cli" in captured.err


def test_a_command_run_as_a_process_writes_only_json_to_stdout(tmp_path):
    """As a real process, with debug logs on, stdout is exactly the command's JSON."""
    env = {
        "EMBEDDING_PROVIDER": "fake",
        "LOCAL_PROFILE": "m1-max-64",
        "LOG_LEVEL": "DEBUG",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "PATH": str(Path(sys.executable).parent),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "nexo.cli", "models"],
        env=env,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(proc.stdout)["local_profile"] == "m1-max-64"
    assert "INFO nexo.cli models (LOCAL_PROFILE=m1-max-64" in proc.stderr
