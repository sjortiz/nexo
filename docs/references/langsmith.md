---
type: Reference
title: LangSmith
description: LangChain's observability, evaluation and deployment platform - what each part offers, where it overlaps Nexo, and its role as an opt-in trace add-on.
resource: https://docs.langchain.com/langsmith
tags: [reference, observability, evaluation, langsmith]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: otel
    resource: https://docs.langchain.com/langsmith/trace-with-opentelemetry
    title: Trace with OpenTelemetry
  - id: eval
    resource: https://docs.langchain.com/langsmith/code-evaluator
    title: Evaluation with code evaluators
  - id: pairwise
    resource: https://docs.langchain.com/langsmith/evaluate-pairwise
    title: Pairwise evaluation
  - id: queues
    resource: https://docs.langchain.com/langsmith/annotation-queues
    title: Annotation queues
  - id: agent-server
    resource: https://docs.langchain.com/langsmith/agent-server
    title: Agent Server
  - id: redaction
    resource: https://docs.langchain.com/langsmith/otel-gateway-trace-redaction
    title: OTel gateway trace redaction
  - id: privacy
    resource: https://docs.langchain.com/langsmith/data-storage-and-privacy
    title: Data storage and privacy
  - id: openevals
    resource: https://github.com/langchain-ai/openevals
    title: openevals
---

# Products

| Part | What it gives | Overlap with Nexo |
|---|---|---|
| Observability | Trace UI; ingests OpenTelemetry at `https://api.smith.langchain.com/otel` (headers `x-api-key`, `Langsmith-Project`) and maps `gen_ai.*` attributes.[^otel] | Opt-in add-on: `make up LANGSMITH=1` adds a `traces/langsmith` collector pipeline behind the content stripping. SigNoz is the default backend. |
| Evaluation | Datasets, `evaluate()` / `aevaluate()`, custom code evaluators, `evaluate_comparative` for pairwise, experiment comparison UI.[^eval][^pairwise] `openevals` ships LLM-as-judge prompts (groundedness, helpfulness, retrieval relevance) usable with a vLLM judge.[^openevals] | Replaces the hand-written eval loop, reporting and generic judges in `evaluation.py`. |
| Feedback and annotation queues | `create_feedback` on runs; review queues that add examples to datasets.[^queues] | Complements, not replaces, the compliance interrupt: approvals can be copied as feedback. |
| Prompt Hub | Versioned prompts pulled at runtime. | Nexo keeps prompts in git (reviewed, no runtime dependency). |
| Deployment (Agent Server) | Threads, runs, interrupt/resume API, task queue, cron, Postgres checkpointer.[^agent-server] | Would replace `Runtime`; self-hosting requires an Enterprise plan. |
| Studio | Visual debugging of the graph with `langgraph dev`. | No overlap; useful locally. |

# Data handling

LangSmith Cloud regions: US, EU, APAC (GCP) and US (AWS). The documentation lists SOC 2 Type II, HIPAA
and GDPR but does not document a BAA; confirm one before sending PHI. Masking happens client-side
(`LANGSMITH_HIDE_INPUTS`, `LANGSMITH_HIDE_OUTPUTS`, anonymizers) or, for OTLP, in a collector
redaction processor.[^privacy][^redaction] Self-hosted LangSmith is an Enterprise add-on.

The add-on drops Nexo's `nexo.content` span event (a run's question and answer); LangSmith receives
only the request span's `nexo.content` verdict (`released (internal)`, `withheld (pii)`), never the text.

How Nexo uses it: an opt-in trace add-on ([ADR-0014](../decisions/adr-0014-langfuse-default-trace-backend.md));
evaluation and Studio options are discussed in [ADR-0011](../decisions/adr-0011-langsmith-adoption.md).

[^otel]: Trace with OpenTelemetry
[^eval]: Evaluation with code evaluators
[^pairwise]: Pairwise evaluation
[^queues]: Annotation queues
[^agent-server]: Agent Server
[^redaction]: OTel gateway trace redaction
[^privacy]: Data storage and privacy
[^openevals]: openevals
