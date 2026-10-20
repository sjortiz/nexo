---
type: Reference
title: Observability
description: Span catalog, metrics, content-capture policy and log correlation, following the OpenTelemetry GenAI conventions.
resource: ../../src/nexo/telemetry.py
tags: [operations, observability, opentelemetry, logging]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: telemetry
    resource: ../../src/nexo/telemetry.py
    title: Telemetry setup
  - id: logging
    resource: ../../src/nexo/logging_setup.py
    title: Logging setup
  - id: semconv
    resource: ../references/opentelemetry-genai-conventions.md
    title: OpenTelemetry GenAI semantic conventions
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
  - id: addons
    resource: ../../deploy/otel-addons/
    title: Collector add-on pipelines
  - id: langfuse
    resource: ../references/langfuse.md
    title: Langfuse
---

# Trace shape

One investigation, start to review, is one trace per call, tied together by
`gen_ai.conversation.id` = LangGraph thread id:[^semconv]

```
invoke_workflow nexo-investigation           INTERNAL  gen_ai.operation.name=invoke_workflow
├─ langgraph.node router                     INTERNAL  langgraph.state_update.keys=[...]
│  └─ chat {model}                           CLIENT    router call to vLLM (GenAI instrumentation)
├─ langgraph.node investigator               INTERNAL
│  ├─ retrieval nexo-chunks                  INTERNAL  seed search (nexo.retrieval.mode=hybrid|vector|keyword)
│  └─ invoke_agent relationship-investigator INTERNAL  Strands
│     └─ execute_event_loop_cycle            INTERNAL  Strands, one per turn
│        ├─ chat                             INTERNAL  Strands model wrapper
│        │  └─ chat {model}                  CLIENT    the vLLM HTTP call
│        ├─ execute_tool query_knowledge_graph  INTERNAL  nexo.graph.rows, nexo.graph.truncated
│        │  └─ agent_query                   CLIENT    db.system.name=neo4j, db.namespace, server.*
│        └─ execute_tool fetch_vector_chunks INTERNAL  nexo.chunks.returned / withheld
│           └─ chunk_store get_by_ids        INTERNAL
└─ langgraph.node compliance                 INTERNAL  langgraph.interrupted=true while awaiting review
```

Ingestion adds `ingest.document`, one `ingest.extract_triples` per chunk (each with its
`chat {model}` and resolution calls), `embeddings {model}` and the `replace_source` /
`find_candidates` Neo4j spans.

# Metrics

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, a MeterProvider exports OTLP metrics. The GenAI
instrumentation records `gen_ai.client.operation.duration` and `gen_ai.client.token.usage` per vLLM
call; Strands adds agent and tool metrics. The GenAI metric names are still in development and are
expected to change upstream.

# Content capture

| Setting | Default | Effect |
|---|---|---|
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `no_content` | `span_only` / `span_and_event` put prompts, outputs and agent Cypher (`db.query.text`) on spans. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | derived | Latest GenAI conventions for Strands; with capture off, adds `gen_ai_unredacted_attributes=` so Strands redacts every content attribute. |

Strands records message content in span events by default; without the redaction token a question
and graph results (names, possibly PHI) reach the trace backend. A test asserts that nothing leaks
with capture off, and that content does appear with capture on.[^telemetry] The collector also deletes
content attributes before export as a second line.[^collector]

# Logs

Logs go to stdout through one handler (`LOG_FORMAT=text|json`, `LOG_LEVEL`). Every record carries
`trace_id` and `span_id` of the current span, so a log line leads to its trace. CLI results and
evaluation rows are logged with a structured `result` field.[^logging]

# Backends

The application exports OTLP to the collector; the collector chooses the backends
([ADR-0014](../decisions/adr-0014-langfuse-default-trace-backend.md)).

| Backend | How | Where |
|---|---|---|
| Langfuse (default) | `make up`; OTLP/HTTP to `/api/public/otel`, Basic auth with the `nexo` project keys, `x-langfuse-ingestion-version: 4`[^collector] | http://localhost:3000; log in with `LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD` from `.env` |
| Jaeger (add-on) | `make up JAEGER=1`; pipeline `traces/jaeger`[^addons] | http://localhost:16686 |
| LangSmith (add-on) | `make up LANGSMITH=1` with `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env`; pipeline `traces/langsmith` | LangSmith project |
| Prometheus metrics | always | http://127.0.0.1:8889/metrics |

Add-ons can be combined. Each one runs the same content-stripping processors as the main pipeline; a
disabled add-on loads `deploy/otel-addons/none.yaml`. Do not enable `LANGSMITH_TRACING` in the
application, or traces are duplicated.

In Langfuse, GenAI spans appear as GENERATION observations (model, token usage), embeddings as
EMBEDDING and the rest as SPAN. Langfuse v4's read API is `/api/public/v2/observations`.[^langfuse]

Without containers, leave `OTEL_EXPORTER_OTLP_ENDPOINT` empty (logs only), or run just the collector
and Langfuse containers.

[^telemetry]: Telemetry setup
[^logging]: Logging setup
[^semconv]: OpenTelemetry GenAI semantic conventions
[^collector]: Collector configuration
[^addons]: Collector add-on pipelines
[^langfuse]: Langfuse
