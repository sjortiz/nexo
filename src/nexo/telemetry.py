"""OpenTelemetry wiring, following the OpenTelemetry GenAI semantic conventions.

One global TracerProvider/MeterProvider serves every span source, so a trace
shows the whole request:

* `invoke_workflow nexo-investigation` - one per graph run (service.Runtime),
  with `gen_ai.conversation.id` = LangGraph thread id.
* `langgraph.node <name>` - one per node, recording the state keys it changed.
* `invoke_agent` / `execute_tool` / `chat` - emitted by Strands for the agent
  loop (latest GenAI conventions via OTEL_SEMCONV_STABILITY_OPT_IN).
* `chat {model}` / `embeddings {model}` CLIENT spans + GenAI metrics - the
  official OpenTelemetry GenAI instrumentation of the openai SDK, which every
  vLLM call goes through (LangChain ChatOpenAI/OpenAIEmbeddings and Strands).
* Database CLIENT spans for Neo4j and chunk-store spans (`retrieval ...`).

Message content stays off spans unless
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT is span_only/span_and_event.
LangSmith is optional and orthogonal: LANGSMITH_TRACING=true.
"""

from __future__ import annotations

import copy
import getpass
import hashlib
import hmac
import json
import logging
import os
import sys
import threading
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import _logs, baggage, metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span, Status, StatusCode

from nexo.config import Settings

TRACER_NAME = "nexo"
# One per process: a CLI run or an MCP server (a Claude Code session).
SESSION_ID = uuid.uuid4().hex
QUERY_TEXT_MAX = 2000


_setup_lock = threading.Lock()
# The process-wide providers, once installed: "providers" -> (tracer, meter), "logs" -> logger.
_INSTALLED: dict[str, Any] = {}


class OtelLogHandler(LoggingHandler):
    """Export log records over OTLP, keeping answer payloads out unless content capture is on.

    A record's `content` (the texts service.content_gate released for a run) is
    exported as one attribute per text; the gate has already decided.
    """

    def __init__(self, logger_provider: LoggerProvider, capture_content: bool) -> None:
        super().__init__(logger_provider=logger_provider)
        self._capture_content = capture_content

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("opentelemetry"):
            return  # the exporter's own logs would feed back into the export
        if hasattr(record, "result") or hasattr(record, "content"):
            record = copy.copy(record)  # other handlers still see the original
        if hasattr(record, "result"):
            if self._capture_content:
                record.result = json.dumps(record.result, default=str)
            else:
                del record.result
        if hasattr(record, "content"):
            content = record.content or {}
            del record.content
            record.__dict__.update({k: str(v) for k, v in content.items()})
        super().emit(record)


class BaggageAttributes(SpanProcessor):
    """Copy baggage entries under a prefix onto each span as it starts.

    `nexo eval` puts a Langfuse experiment's ids in baggage (nexo.experiments), so every
    span of a case's trace carries them. Only ids go in baggage: it can reach outbound
    request headers.
    """

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        for key, value in baggage.get_all(parent_context).items():
            if key.startswith(self._prefix):
                span.set_attribute(key, str(value))


def apply_genai_env(settings: Settings) -> None:
    """Export GenAI telemetry switches for libraries that read os.environ.

    Explicit environment values win. Strands is told to emit the latest GenAI
    conventions and, unless content capture is on, to redact every content
    attribute (an empty `gen_ai_unredacted_attributes=` list).
    """
    os.environ.setdefault(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
        settings.otel_instrumentation_genai_capture_message_content,
    )
    opt_in = settings.otel_semconv_stability_opt_in or (
        "gen_ai_latest_experimental"
        if settings.capture_content
        else "gen_ai_latest_experimental,gen_ai_unredacted_attributes="
    )
    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", opt_in)


def setup_telemetry(settings: Settings) -> TracerProvider:
    """Install the process-wide providers and instrumentation once (idempotent)."""
    with _setup_lock:
        if "providers" in _INSTALLED:
            return _INSTALLED["providers"][0]
        apply_genai_env(settings)
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "deployment.environment.name": settings.deployment_environment,
            }
        )
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BaggageAttributes("langfuse."))
        meter_provider: MeterProvider | None = None
        if settings.otel_exporter_otlp_endpoint:
            base = settings.otel_exporter_otlp_endpoint.rstrip("/")
            tracer_provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
            )
            meter_provider = MeterProvider(
                resource=resource,
                metric_readers=[
                    PeriodicExportingMetricReader(
                        OTLPMetricExporter(endpoint=f"{base}/v1/metrics")
                    )
                ],
            )
            metrics.set_meter_provider(meter_provider)
            if settings.otel_logs_exporter == "otlp":
                _install_log_export(settings, resource, f"{base}/v1/logs")
        # stderr: stdout may carry a protocol (MCP stdio)
        if settings.otel_console_export:
            tracer_provider.add_span_processor(
                SimpleSpanProcessor(ConsoleSpanExporter(out=sys.stderr))
            )
        trace.set_tracer_provider(tracer_provider)
        if settings.otel_instrument_openai:
            instrument_openai(tracer_provider, meter_provider)
        _INSTALLED["providers"] = (tracer_provider, meter_provider)
        return tracer_provider


def _install_log_export(settings: Settings, resource: Resource, endpoint: str) -> None:
    """Send root-logger records over OTLP from now on."""
    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint))
    )
    _logs.set_logger_provider(logger_provider)
    logging.getLogger().addHandler(
        OtelLogHandler(logger_provider, settings.capture_content)
    )
    _INSTALLED["logs"] = logger_provider


def instrument_openai(
    tracer_provider: TracerProvider | None = None,
    meter_provider: MeterProvider | None = None,
) -> None:
    """Instrument the openai client once, on the given providers."""
    instrumentor = OpenAIInstrumentor()
    if not instrumentor.is_instrumented_by_opentelemetry:
        kwargs: dict[str, Any] = {"tracer_provider": tracer_provider}
        if meter_provider is not None:
            kwargs["meter_provider"] = meter_provider
        instrumentor.instrument(**kwargs)


def shutdown_telemetry() -> None:
    """Flush pending spans and metrics; call before process exit (CLI, worker shutdown)."""
    if "providers" in _INSTALLED:
        tracer_provider, meter_provider = _INSTALLED["providers"]
        tracer_provider.shutdown()
        if meter_provider is not None:
            meter_provider.shutdown()
    if "logs" in _INSTALLED:
        _INSTALLED["logs"].shutdown()


def get_tracer() -> trace.Tracer:
    """Return the Nexo tracer from the current tracer provider."""
    return trace.get_tracer(TRACER_NAME)


@contextmanager
def traced_span(
    name: str, attributes: Mapping[str, Any] | None = None, **kwargs: Any
) -> Iterator[Span]:
    """Start a current span; exceptions are recorded and set ERROR status."""
    with get_tracer().start_as_current_span(
        name, attributes=clean_attributes(attributes or {}), **kwargs
    ) as span:
        yield span


def record_span_error(
    span: Span, exc: Exception, *, capture_content: bool = False
) -> None:
    """Mark a failure without exporting a potentially content-bearing exception."""
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR, "storage operation failed"))
    if capture_content:
        span.record_exception(exc)


@contextmanager
def query_span(
    name: str,
    attributes: Mapping[str, Any] | None = None,
    *,
    capture_content: bool = False,
    **kwargs: Any,
) -> Iterator[Span]:
    """Trace storage operations with safe errors; content capture is explicitly opt-in.

    Driver messages can quote query literals. Disable the SDK's automatic event
    and status description, then set only a generic status and exception type.
    """
    with traced_span(
        name,
        attributes,
        record_exception=False,
        set_status_on_exception=False,
        **kwargs,
    ) as span:
        try:
            yield span
        except Exception as exc:
            record_span_error(span, exc, capture_content=capture_content)
            raise


def caller_attributes(settings: Settings, user: str | None = None) -> dict[str, str]:
    """Who asked: user.id (given, NEXO_USER or the login name) and this process's session."""
    return {
        "user.id": user or settings.nexo_user or _login_name(),
        "session.id": SESSION_ID,
    }


def query_attributes(settings: Settings, query: str) -> dict[str, Any]:
    """Which question: its length and a keyed hash always; its text only when opted in.

    The hash (HMAC-SHA256 of the question, case and spacing folded) matches repeated
    questions across traces without revealing them; the key never leaves the process.
    """
    attributes: dict[str, Any] = {"nexo.query.length": len(query)}
    if settings.query_hash_key is not None:
        key = settings.query_hash_key.get_secret_value().encode()
        folded = " ".join(query.lower().split()).encode()
        attributes["nexo.query.hash"] = hmac.new(
            key, folded, hashlib.sha256
        ).hexdigest()[:16]
    if settings.trace_query_text:
        attributes["nexo.query.text"] = query[:QUERY_TEXT_MAX]
    return attributes


def _login_name() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers)
        return "unknown"


def clean_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Drop None values and coerce to OTEL-supported attribute types."""
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, (str, bool, int, float)):
            cleaned[key] = value
        elif isinstance(value, (list, tuple, set, frozenset)):
            cleaned[key] = [str(v) for v in value]
        else:
            cleaned[key] = str(value)
    return cleaned
