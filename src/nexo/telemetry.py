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

import os
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.genai.openai import OpenAIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.trace import Span

from nexo.config import Settings

TRACER_NAME = "nexo"

_setup_lock = threading.Lock()
# The process-wide (tracer, meter) providers, once installed.
_INSTALLED: dict[str, tuple[TracerProvider, MeterProvider | None]] = {}


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
