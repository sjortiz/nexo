---
type: FAQ
title: FAQ
description: Short answers to common questions - which trace backend to use, what Nexo is and is not, open-source status, data leaving your machine, model quality and clearance.
tags: [operations, faq]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T15:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: adr-0015
    resource: ../decisions/adr-0015-signoz-default-observability.md
    title: "ADR-0015: SigNoz as the default observability backend"
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

# Which setup should I choose?

Start from your hardware: [Choosing a setup](decision-guide.md) has a decision matrix per choice
(preset, one or two machines, model serving, observability add-ons, memory per combination, how to
run the services).

# Which observability backend should I use: SigNoz, Langfuse, Jaeger or LangSmith?

They answer different questions, and all of them receive the same telemetry from the same collector,
so switching never changes Nexo itself.[^adr-0015]

| Backend | Answers | Start it | Use it when |
|---|---|---|---|
| SigNoz (default) | What is happening, and is it healthy? Traces, service map, metrics (latency, tokens, eval scores), logs, host and container metrics, dashboards, alerts | `make up` | Day-to-day operation: one place for everything |
| Langfuse (add-on) | How well did the models do? Datasets, experiments, scores, LLM-as-judge, annotation queues | `make up LANGFUSE=1` | Evaluation and fine-tuning work |
| Jaeger (add-on) | Where did the time go? Every span's timing | `make up JAEGER=1` | You want a light span-only UI |
| LangSmith (add-on) | LangChain's hosted view and evaluation tooling | `make up LANGSMITH=1` | Your team already uses LangSmith or wants its evaluators |

SigNoz, Langfuse and Jaeger run on your machine. LangSmith is a cloud service: traces leave
your machine (without message content), so sensitive data needs a BAA or LangSmith's Enterprise
self-hosting. Details: [Observability](observability.md);[^observability] memory and cost side by side:
[Choosing a setup](decision-guide.md#4-which-observability-to-turn-on).

# How do I collect logs?

Set `OTEL_LOGS_EXPORTER=otlp` (the local template already does). Log records go through the collector
to SigNoz with their trace ids, so you can jump from a log line to its trace.
Structured `result` payloads (which can hold answers) are left out unless content capture is on.
Logs always go to stderr as well; stdout carries only the command's JSON.[^observability]

# Why does SigNoz not replace Langfuse?

SigNoz covers operations: traces, metrics, logs, dashboards and alerts. It has no LLM evaluation
workflow (datasets, experiments, scores per answer, annotation queues), which is where answer quality is
judged. Both are open source and free to self-host, so Nexo runs SigNoz by default and offers Langfuse
as an add-on.[^adr-0015]

# Is Nexo a drop-in, self-hosted RAG?

Close, as a scaffold. It covers ingestion, embeddings, Neo4j storage, hybrid search, multi-hop
traversal with citations, human review, and querying from Claude Code over a read-only MCP
server.[^mcp] Not there yet: ingestion takes Markdown and plain text only, documents cannot be
deleted, and it serves one tenant.

# Is it fully open source?

Nexo's code is MIT, and its components are open source (LangGraph, Strands Agents, Neo4j Community,
Ollama, SigNoz, Langfuse's core, SeaweedFS). The local profiles use models with open licenses (Qwen,
Apache-2.0; bge-m3, MIT). The default model for vLLM GPU servers, Llama 3.3, is under Meta's
community license, which is not OSI-approved; any OpenAI-compatible model can replace it.

# Does Nexo send my data anywhere?

Not by default. Models run in Ollama or vLLM on your hardware, SigNoz and Langfuse are self-hosted with their usage
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

Chunk text above the caller's clearance is withheld, or shown with detected PII masked (`[EMAIL]`)
when that PII is the only reason it is above. Graph tools drop chunk text from the rows they return
and refuse Cypher that reads it by name, and whole nodes above the clearance are redacted. On Neo4j
Community, a query that returns a plain value (`RETURN p.name`) cannot be traced back to its node, so a
name can still appear. Neo4j Enterprise closes this with role-based access on the `:PII` and `:PHI`
labels; on Community, human review is the remaining control.[^adr-0006]

[^adr-0015]: ADR-0015: SigNoz as the default observability backend
[^observability]: Observability
[^local-setup]: How local setup works
[^adr-0006]: ADR-0006: Sensitivity and caller clearance
[^mcp]: MCP server
