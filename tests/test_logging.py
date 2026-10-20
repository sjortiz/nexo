"""Logging setup: trace-correlated records, JSON and text formatters, handler install."""

from __future__ import annotations

import json
import logging

import pytest

from nexo.logging_setup import (
    JsonFormatter,
    TextFormatter,
    TraceContextFilter,
    configure_logging,
)
from nexo.telemetry import get_tracer


def _record(message="done", **extra) -> logging.LogRecord:
    record = logging.makeLogRecord(
        {
            "name": "nexo.cli",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": message,
            **extra,
        }
    )
    TraceContextFilter().filter(record)
    return record


def test_records_carry_the_current_trace_and_nothing_outside_a_span():
    """Records get the active span's trace and span ids, and empty ids outside a span."""
    outside = _record()
    assert outside.trace_id == "" and outside.span_id == ""
    with get_tracer().start_as_current_span("work") as span:
        inside = _record()
    context = span.get_span_context()
    assert inside.trace_id == format(context.trace_id, "032x")
    assert inside.span_id == format(context.span_id, "016x")


def test_json_lines_include_structured_result_and_trace():
    """JSON lines carry message, logger, structured result and trace id, not raw record fields."""
    with get_tracer().start_as_current_span("work"):
        record = _record(
            "thread t1: completed", result={"thread_id": "t1", "status": "completed"}
        )
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "thread t1: completed" and entry["logger"] == "nexo.cli"
    assert entry["result"] == {"thread_id": "t1", "status": "completed"}
    assert len(entry["trace_id"]) == 32
    assert (
        "args" not in entry and "msg" not in entry
    )  # standard LogRecord fields are not leaked


def test_text_format_appends_result_only_when_present():
    """Text lines end with the message and append the result as JSON only when one is given."""
    plain = TextFormatter().format(_record("hello"))
    assert plain.endswith("INFO nexo.cli hello")
    with_result = TextFormatter().format(_record("scored", result={"precision": 0.5}))
    assert with_result.splitlines()[0].endswith("scored")
    assert json.loads("\n".join(with_result.splitlines()[1:])) == {"precision": 0.5}


@pytest.mark.parametrize(
    ("fmt", "formatter"), [("json", JsonFormatter), ("text", TextFormatter)]
)
def test_configure_logging_installs_one_handler(settings, fmt, formatter):
    """Repeated configuration leaves one handler with the chosen formatter and quiets httpx."""
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        settings.log_format, settings.log_level = fmt, "debug"
        configure_logging(settings)
        configure_logging(settings)  # idempotent: no duplicate handlers
        assert len(root.handlers) == 1 and isinstance(
            root.handlers[0].formatter, formatter
        )
        assert root.level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
    finally:
        root.handlers[:], _ = saved[0], root.setLevel(saved[1])
