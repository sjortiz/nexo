"""Logging configuration: one handler, trace-correlated records.

Every record carries the current OpenTelemetry trace and span ids, so a log
line can be found from its trace and vice versa. Structured payloads (CLI
results, eval rows) go in `extra={"result": ...}` rather than the message.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from nexo.config import Settings

_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class TraceContextFilter(logging.Filter):
    """Adds `trace_id` / `span_id` (hex, empty outside a span) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x") if context.is_valid else ""
        record.span_id = format(context.span_id, "016x") if context.is_valid else ""
        return True


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if getattr(record, "trace_id", ""):
            line += f" trace_id={record.trace_id} span_id={record.span_id}"
        if hasattr(record, "result"):
            line += "\n" + json.dumps(record.result, indent=2, default=str)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for log pipelines."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry.update({k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS and v not in ("", None)})
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(settings: Settings, stream=None) -> None:
    """One handler on stdout, or another stream (e.g. stderr when stdout carries MCP stdio)."""
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.addFilter(TraceContextFilter())
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpcore", "neo4j", "openai"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
