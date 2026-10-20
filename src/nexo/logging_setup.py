"""Logging configuration: one handler on stderr, trace-correlated records.

Every record carries the current OpenTelemetry trace and span ids, so a log
line can be found from its trace and vice versa. Logs go to stderr so stdout
carries only a command's output (CLI results and eval rows, as JSON, or the
MCP stdio protocol). Records also carry that payload in `extra={"result": ...}`
for OTLP export; the stderr formatters leave it out, since stdout has it.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.instrumentation.logging.handler import LoggingHandler

from nexo.config import Settings

# `result` is the command's stdout output and `content` a run's gated question and
# answer; only the OTLP handler exports them.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "result",
    "content",
}


# One public method is logging's interface.
class TraceContextFilter(logging.Filter):  # pylint: disable=too-few-public-methods
    """Adds `trace_id` / `span_id` (hex, empty outside a span) to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        context = trace.get_current_span().get_span_context()
        record.trace_id = format(context.trace_id, "032x") if context.is_valid else ""
        record.span_id = format(context.span_id, "016x") if context.is_valid else ""
        return True


class TextFormatter(logging.Formatter):
    """Human-readable lines with trace ids."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if getattr(record, "trace_id", ""):
            line += f" trace_id={record.trace_id} span_id={record.span_id}"
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
        entry.update(
            {
                k: v
                for k, v in vars(record).items()
                if k not in _STANDARD_ATTRS and v not in ("", None)
            }
        )
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(settings: Settings, stream=None) -> None:
    """One handler on stderr (or `stream`): stdout is the command's output."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.addFilter(TraceContextFilter())
    handler.setFormatter(
        JsonFormatter() if settings.log_format == "json" else TextFormatter()
    )
    root = logging.getLogger()
    # Keep OTLP log export if telemetry was set up first (OTEL_LOGS_EXPORTER=otlp).
    root.handlers[:] = [
        handler,
        *(h for h in root.handlers if isinstance(h, LoggingHandler)),
    ]
    root.setLevel(settings.log_level.upper())
    for noisy in ("httpx", "httpcore", "neo4j", "openai"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
