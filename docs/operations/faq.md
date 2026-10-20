---
type: FAQ
title: FAQ
description: Short answers to common questions - which trace backend to use, what Nexo is and is not, open-source status, data leaving your machine, model quality and clearance.
tags: [operations, faq]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T15:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: adr-0014
    resource: ../decisions/adr-0014-langfuse-default-trace-backend.md
    title: "ADR-0014: Langfuse as the default trace backend"
  - id: observability
    resource: observability.md
    title: Observability
  - id: local-setup
    resource: local-setup.md
    title: How local setup works
  - id: adr-0006
    resource: ../decisions/adr-0006-sensitivity-and-caller-clearance.md
    title: "ADR-0006: Sensitivity and caller clearance"
  - id: mcp
    resource: ../components/mcp-server.md
    title: MCP server
---

# Which trace backend should I use: Langfuse, Jaeger or LangSmith?

They answer different questions, and all three receive the same traces from the same collector, so
switching never changes Nexo itself.[^adr-0014]

| Backend | Answers | Start it | Use it when |
|---|---|---|---|
| Langfuse (default) | What did the models do, and how well? Generations with token counts, one trace per question, scores and datasets | `make up` | Tuning prompts, comparing models, checking answers and token use |
| Jaeger (add-on) | Where did the time go? Every span's timing, including Neo4j, embeddings and the chunk store | `make up JAEGER=1` | A request is slow or a component fails; one light container |
| LangSmith (add-on) | LangChain's hosted view and evaluation tooling | `make up LANGSMITH=1` | Your team already uses LangSmith or wants its evaluators |
| Grafana LGTM (add-on) | Traces, metrics and logs together, linked by trace id | `make up LGTM=1` | You want stored metrics (latency, tokens, eval scores) and searchable logs |

Langfuse, Jaeger and Grafana LGTM run on your machine. LangSmith is a cloud service: traces leave
your machine (without message content), so sensitive data needs a BAA or LangSmith's Enterprise
self-hosting. Details: [Observability](observability.md).[^observability]

# How do I collect logs?

Set `OTEL_LOGS_EXPORTER=otlp` and start the Grafana LGTM add-on (`make up LGTM=1`). Log records go
through the collector to Loki with their trace ids, so Grafana jumps from a log line to its trace.
Structured `result` payloads (which can hold answers) are left out unless content capture is on.
Logs always go to stdout as well.[^observability]

# Why not one observability platform, such as SigNoz?

A general platform covers operations well: traces, metrics, logs, dashboards and alerts. It does not
replace Langfuse's evaluation workflow (datasets, experiments, scores per answer, annotation queues),
which is where answer quality is judged. Both are open source and free to self-host, so Nexo keeps
Langfuse by default and offers Grafana LGTM for operations; for production, SigNoz or the full Grafana
stack receive the same OpenTelemetry data through the collector, with no change to Nexo.[^adr-0014]

# Is Nexo a drop-in, self-hosted RAG?

Close, as a scaffold. It covers ingestion, embeddings, Neo4j storage, hybrid search, multi-hop
traversal with citations, human review, and querying from Claude Code over a read-only MCP
server.[^mcp] Not there yet: ingestion takes Markdown and plain text only, documents cannot be
deleted, and it serves one tenant.

# Is it fully open source?

Nexo's code is MIT, and its components are open source (LangGraph, Strands Agents, Neo4j Community,
Ollama, Langfuse's core, SeaweedFS). The local profiles use models with open licenses (Qwen,
Apache-2.0; bge-m3, MIT). The default model for vLLM GPU servers, Llama 3.3, is under Meta's
community license, which is not OSI-approved; any OpenAI-compatible model can replace it.

# Does Nexo send my data anywhere?

Not by default. Models run in Ollama or vLLM on your hardware, Langfuse is self-hosted with its usage
telemetry off, and the collector removes prompts, answers and query text from traces before export.
Only the LangSmith add-on sends traces to a cloud service.[^observability]

# Why does extraction get some facts wrong, and how do I use a bigger model?

Small local models (the 9B model on 16-24 GB machines) sometimes reverse a relation or pick one the
quote does not support. Every fact keeps its source chunk, the agent reads that text before
answering, and a person reviews the answer. For better extraction, give the extraction role a larger
model: set `EXTRACTION_MODEL` (and `EXTRACTION_BASE_URL` if it runs elsewhere); explicit settings win
over the hardware profile.[^local-setup] Then run `nexo models --check`.

# What does "structured output failed" from `nexo models --check` mean?

The server lists the model but cannot constrain its output to a JSON schema, so extraction and
routing would fail. On Apple Silicon this happens with Ollama's MLX builds (tags ending in `-mlx`,
`-nvfp4`, and some defaults). Use a GGUF tag such as `qwen3.5:9b-q4_K_M`; the profiles pin these.

# Can a caller with lower clearance see the names of sensitive entities?

The text of chunks above the caller's clearance is always withheld, and whole nodes above it are
redacted. On Neo4j Community, a query that returns a plain value (`RETURN p.name`) cannot be traced
back to its node, so a name can still appear. Neo4j Enterprise closes this with role-based access on
the `:PII` and `:PHI` labels; on Community, human review is the remaining control.[^adr-0006]

[^adr-0014]: ADR-0014: Langfuse as the default trace backend
[^observability]: Observability
[^local-setup]: How local setup works
[^adr-0006]: ADR-0006: Sensitivity and caller clearance
[^mcp]: MCP server
