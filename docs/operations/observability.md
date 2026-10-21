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
  - id: service
    resource: ../../src/nexo/service.py
    title: Content gate (content_gate)
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
  - id: addons
    resource: ../../deploy/otel-addons/
    title: Collector add-on pipelines
  - id: signoz
    resource: ../references/signoz.md
    title: SigNoz
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
│        ├─ execute_tool query_knowledge_graph  INTERNAL  nexo.graph.rows, nexo.graph.truncated, error.type
│        │  └─ agent_query                   CLIENT    db.system.name=neo4j, db.namespace, server.*
│        └─ execute_tool fetch_vector_chunks INTERNAL  nexo.chunks.returned / withheld
│           └─ chunk_store get_by_ids        INTERNAL
└─ langgraph.node compliance                 INTERNAL  langgraph.interrupted=true while awaiting review
```

Ingestion adds `ingest.document`, one `ingest.extract_triples` per chunk (each with its
`chat {model}` and resolution calls), `embeddings {model}` and the `replace_source` /
`find_candidates` Neo4j spans.

# Failed tool calls

A tool call that returns an error to the model (unknown entity key, Cypher syntax error, budget
exhausted, store down) is a failed call: its `execute_tool` span has status Error,
`gen_ai.tool.status=error` and an `error.type`. Compliance counts the calls that ran in `tool_errors`
and calls refused for budget under `budget_exhausted`, so failed spans = `tool_errors` + refusals.

| `error.type` | Cause |
|---|---|
| `unknown_key` | `find_paths` or `entity_neighbours` got a key that matches no visible entity |
| `same_entity` | `find_paths` got the same key twice |
| `unsafe_cypher` | the read-only guard rejected the query |
| `Neo.ClientError...` | the Neo4j error code (syntax error, timeout, access mode) |
| `graph_unavailable` | Neo4j unreachable after retries |
| `budget_exhausted` | the tool budget was used up (a refused call) |
| `result_too_large` | the result did not fit `TOOL_RESULT_MAX_CHARS` even with every row dropped |
| exception class name | the chunk store failed, or a bug in the tool: the model gets a generic error, and the traceback goes to the log only |

The span status description is Strands' generic "tool returned error status": the error text the
model received is message content (a Cypher error can quote the query), so it stays off the tool
span unless capture is on.

In SigNoz, Traces Explorer: filter `name LIKE 'execute_tool %'` and `hasError = true` (or
`gen_ai.tool.status = error`), group by `error.type`. Strands' tool metrics agree with the spans:
`strands.tool.error_count` (by `tool_name`) counts the same calls, so a dashboard panel can chart
either.

## Failed query and store spans

A span that ends in an exception (`agent_query`, a chunk store read, any `traced_span`) is `ERROR`
with `error.type` set to the exception class and the class name as its status description, and
nothing else: a Neo4j error quotes the offending query line, literals included, and a store error
can quote a path, so the message and the `exception` event are recorded only when
`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` is `span_only` or `span_and_event`
([ADR-0009](../decisions/adr-0009-opentelemetry-genai-conventions.md)). The full text is still in
the process log at `ERROR` level.


# Metrics

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, a MeterProvider exports OTLP metrics. The GenAI
instrumentation records `gen_ai.client.operation.duration` and `gen_ai.client.token.usage` per vLLM
call; Strands adds agent and tool metrics. The GenAI metric names are still in development and are
expected to change upstream. The collector exposes them on `127.0.0.1:8889`; SigNoz stores
them, so they can be charted, including `nexo eval` scores. The collector also reports host and
container metrics (see Backends).

# Content capture

| Setting | Default | Effect |
|---|---|---|
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `no_content` | `span_only` / `span_and_event` put prompts, outputs and agent Cypher (`db.query.text`) on spans. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | derived | Latest GenAI conventions for Strands; with capture off, adds `gen_ai_unredacted_attributes=` so Strands redacts every content attribute. |

Strands records message content in span events by default; without the redaction token a question
and graph results (names, possibly PHI) reach the trace backend. A test asserts that nothing leaks
with capture off, and that content does appear with capture on.[^telemetry] The collector also deletes
content attributes before export as a second line.[^collector] A review pause is not recorded as an
exception: its payload holds the question and the draft answer.

# Question and answer logs

Operators need what was asked and what was answered, and most runs touch nothing sensitive, so
each run's question and released answer are exported when the run is no more sensitive than
`telemetry.content` in `nexo.yaml` (`TELEMETRY_CONTENT_MAX_SENSITIVITY`, default `internal`;
[ADR-0009](../decisions/adr-0009-opentelemetry-genai-conventions.md), amended).[^service]

A run's level is the highest of:

- the chunks its agent read (the evidence ledger, `fetched_sensitivity`, that also grounds citations);
- what the sensitive-data detector finds in the question, the released answer, the reviewer's
  comment and the router's reason.

A level Nexo does not rank counts as `phi`, and so does an agent run that failed or timed out without
its ledger; a run that read nothing and in which nothing is found is `public`. Graph rows are not in the ledger: the detector on the answer is what catches personal details
the agent took from them.

| Setting | Runs logged with their content |
|---|---|
| `none` | None: no question or answer is exported (the behaviour before this setting) |
| `public` | Runs that read only public chunks and in which the detector finds nothing |
| `internal` (default) | Also runs that read internal chunks |
| `pii` | Also runs that read PII chunks, or whose question or answer has an email, phone number, SSN and so on |
| `phi` | Every run the detector could classify |

Whatever the setting, nothing is logged without a detector (`SENSITIVE_DATA_PROVIDER=none`) or
when the detector fails, unless content capture is on.

At or below the setting, after each `start` and `review` Nexo logs `thread <id>: content: released
(internal)` on the `nexo.content` logger, kept at INFO whatever `LOG_LEVEL` is so the record always
reaches OTLP (stderr still follows `LOG_LEVEL`); the OTLP record carries `nexo.content.question`,
`nexo.content.answer` (only once released by a reviewer or the review policy, never a draft),
`nexo.content.review_comment` and `nexo.content.routing_reason`, and the request span gets the
same attributes as a `nexo.content` event. Above it the line reads `content: withheld (pii)` and
carries no text. With `telemetry.logs: otlp` the record reaches SigNoz; the span event reaches the
self-hosted trace backends, and the LangSmith add-on drops it.[^addons] The request span's
`nexo.content` attribute holds the verdict either way, and LangSmith receives that verdict, not the
text. The stderr log line never carries the text, but `OTEL_CONSOLE_EXPORT=true` prints spans to
stderr, released `nexo.content` events included.

The gate fails closed. When the detector raises (for example Comprehend Medical is unreachable) the
content is withheld as `withheld (detector failed)`; when there is none, including
`SENSITIVE_DATA_PROVIDER=none`, whose detector finds nothing and so cannot classify, as `withheld (no
detector)`. `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=span_only|span_and_event` still
exports every run's content (`released (content capture on)`). `make check` shows the setting, the
detector and the capture switch in force, read as Nexo reads them (`.env` included).

# Which user and which question

Every request span (`invoke_workflow`) and MCP tool span says who asked and which question, without
the question itself:

| Attribute | Value |
|---|---|
| `user.id` | `nexo ask --user`, the reviewer on a review, else `NEXO_USER`, else the login name |
| `session.id` | One per CLI run or MCP server (a Claude Code session) |
| `gen_ai.conversation.id` | The thread: ties a question to its review |
| `nexo.query.hash` | HMAC-SHA256 of the question (case and spacing folded): the same question gives the same hash; the key (`QUERY_HASH_KEY`, generated by `make env`) stays in the secrets store |
| `nexo.query.length` | Characters in the question |
| `nexo.query.text` | The question, only with `telemetry.queryText: true` in `nexo.yaml` (`TRACE_QUERY_TEXT=true`); kept for self-hosted backends, never sent to LangSmith, and stripped by the collector otherwise |

In SigNoz, filter traces by `user.id` or `nexo.query.hash` to find a user's questions or every time a
question was asked. Apart from `nexo.query.text`, the question reaches a trace only in the
`nexo.content` event, by the run's sensitivity (see [Question and answer logs](#question-and-answer-logs)).

# Logs

Logs go to stderr through one handler (`LOG_FORMAT=text|json`, `LOG_LEVEL`). Every record carries
`trace_id` and `span_id` of the current span, so a log line leads to its trace. Stdout carries only the
command's output: CLI results as JSON (`nexo ask ... | jq`), evaluation rows as one JSON object per
line, and the protocol for `nexo mcp` over stdio. Each result's record also holds it as a `result` field
for OTLP export; the stderr lines leave it out.[^logging]

Opt-in: `OTEL_LOGS_EXPORTER=otlp` also sends records to the collector with the same trace ids. The
`result` field is left out unless content capture is on, and OpenTelemetry's own records are never
exported. A run's question and answer are exported on their own record, by sensitivity (see
[Question and answer logs](#question-and-answer-logs)). SigNoz stores them and links each line to its trace. The setting defaults to `none` in code; the
local template sets `otlp`.

# Dashboard and alerts

`nexo setup up` loads a **Nexo** dashboard and three alerts into SigNoz from
`deploy/signoz/provision/` (headless, as the root user the stack creates; re-running replaces them by
name). The panels are ClickHouse queries over the spans described above:

| Panel | What it answers |
|---|---|
| Questions and how they ended | Runs per operation, status and approval (`not_required` is a router refusal). |
| Question latency | p50 and p95 of `invoke_workflow` for new questions. |
| Time per step | Count, average, p95 and total time of nodes, model calls, tool calls, Neo4j queries and embeddings. |
| Model calls per question | Average `chat` spans per question trace; three or four is typical. |
| Tokens per model | Calls, input and output tokens and the average prompt per model (one model per role). |
| Tool calls and failures | Calls, failed calls and their `error.type` per tool. |
| How investigations stopped | `stop_reason` (`limit_turns` is a turn-limit hit), follow-up calls and empty answers, from `nexo.investigation.*` on the investigator node. |
| Reviews | Answers the review policy approved as `system:auto` against those that waited. |
| Neo4j query time | p95 per kind of graph store call. |

| Alert | Fires when |
|---|---|
| Nexo: slow question | p95 question latency over 15 minutes is above 300 s. |
| Nexo: too many model calls per question | The average is above 12. |
| Nexo: error rate | More than 5% of `nexo` spans end in an error. |

Alerts go to a webhook channel named `nexo`: set `SIGNOZ_ALERT_WEBHOOK` in `.env` to be notified;
without it they still show as firing on SigNoz's Alerts page. Edit the thresholds in
`deploy/signoz/provision/alerts.json` and run `make up` again.

# Backends

The application exports OTLP to the collector; the collector chooses the backends
([ADR-0015](../decisions/adr-0015-signoz-default-observability.md)).

| Backend | How | Where |
|---|---|---|
| SigNoz (default) | `make up`; the collector exports traces, metrics and logs over OTLP gRPC to `signoz-ingester`[^collector] | http://localhost:8080; log in with `SIGNOZ_USER_EMAIL` / `SIGNOZ_USER_PASSWORD` from `~/.config/nexo/config`[^signoz] |
| Langfuse (add-on) | `make up LANGFUSE=1`; pipeline from `deploy/otel-addons/langfuse.yaml`: OTLP/HTTP to `/api/public/otel`, Basic auth with the `nexo` project keys, `x-langfuse-ingestion-version: 4`[^collector] | http://localhost:3000; log in with `LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD` from `~/.config/nexo/config` |
| Jaeger (add-on) | `make up JAEGER=1`; pipeline `traces/jaeger`[^addons] | http://localhost:16686 |
| LangSmith (add-on) | `make up LANGSMITH=1` (or `addons.langsmith` in `nexo.yaml`, with its `project`) and `LANGSMITH_API_KEY` in `.env`; pipeline `traces/langsmith` | LangSmith project |
| Prometheus metrics | always | http://127.0.0.1:8889/metrics |

Add-ons can be combined: `addons:` in `nexo.yaml`, or `LANGFUSE=1`, `JAEGER=1`, `LANGSMITH=1` for one
run. Each one runs the same content-stripping processors as the main pipeline; `nexo setup` merges
the enabled ones into the rendered collector config, and a disabled add-on is not rendered at all. Do not enable `LANGSMITH_TRACING` in the
application, or traces are duplicated.

Host and container metrics: the collector's `host_metrics` receiver (CPU, load, memory, paging and
disk from the container's `/proc`, which reports the host's; root filesystem usage from a read-only
`/hostfs` mount) and `docker_stats` receiver (per-container CPU, memory, network and I/O, from the
container engine's socket) feed SigNoz as an infrastructure view, with `host.name` set to the machine's
name by `nexo setup`. On macOS the host is the Podman VM, not the Mac, and Ollama runs outside it. The
collector runs as root in its container to read the socket (with rootless Podman, the unprivileged
socket owner), with SELinux labeling off for that container so Podman's VM lets it connect.

With `LANGFUSE=1`, in Langfuse, GenAI spans appear as GENERATION observations (model, token usage), embeddings as
EMBEDDING and the rest as SPAN. Langfuse v4's read API is `/api/public/v2/observations`.[^langfuse]

Without containers, leave `OTEL_EXPORTER_OTLP_ENDPOINT` empty (logs only), or run just the collector
and SigNoz containers.

[^telemetry]: Telemetry setup
[^logging]: Logging setup
[^semconv]: OpenTelemetry GenAI semantic conventions
[^service]: Content gate (content_gate)
[^collector]: Collector configuration
[^addons]: Collector add-on pipelines
[^signoz]: SigNoz
[^langfuse]: Langfuse
