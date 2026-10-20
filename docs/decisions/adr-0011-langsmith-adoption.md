---
type: Architecture Decision
title: "ADR-0011: Where LangSmith fits"
description: Use LangSmith as a trace backend and evaluation platform instead of growing custom tooling; keep the workflow, review gate and prompts in Nexo; no PHI until a BAA or self-hosting.
tags: [adr, langsmith, observability, evaluation]
status: draft
decision_status: superseded
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: langsmith
    resource: ../references/langsmith.md
    title: LangSmith
---

# Context

Superseded by [ADR-0014](adr-0014-langfuse-default-trace-backend.md): Langfuse is the default trace backend; LangSmith is an opt-in add-on.

Nexo grew its own evaluation loop and LLM judges, and could grow its own trace UI, feedback store and
review API. LangSmith already provides these. The data may include PII/PHI, and LangSmith's
documentation does not show a BAA.[^langsmith]

# Proposal

| Area | Recommendation | Code impact |
|---|---|---|
| Trace UI | **Adopt now.** Add LangSmith as a second OTLP exporter in the collector, behind its redaction processors. Leave `LANGSMITH_TRACING` off in the application (no duplicate traces). | Config only (`deploy/otel-collector.yaml`). |
| Evaluation | **Adopt now, on de-identified data.** Datasets + `evaluate()`; `openevals` judges with a vLLM judge model; `evaluate_comparative` for A/B prompts. | Replace the `nexo eval` loop and the generic judges; keep extraction and retrieval metrics as custom evaluators. |
| Local debugging | **Adopt now.** Studio with `langgraph dev`. | None. |
| Review feedback | **Later.** Copy compliance decisions to LangSmith feedback and annotation queues to grow datasets; graph state stays the system of record. | Small hook after `review()`. |
| Prompt Hub | **Don't adopt.** Prompts stay in git: reviewed, versioned with code, no runtime dependency. | None. |
| Agent Server deployment | **Don't adopt yet.** Would replace `Runtime`, but self-hosting needs Enterprise and Cloud would store checkpoints with PHI. Revisit for a multi-user API or scheduled runs. | None. |

# Decisions needed

1. LangSmith plan and region (Plus is enough for dev traces and evaluation).
2. Whether production traces go to LangSmith at all: only with a signed BAA or self-hosted
   Enterprise, and only with content capture off.
3. Approval to replace `nexo eval` with LangSmith experiments (removes ~100 lines; adds a SaaS
   dependency for evaluation runs).

# Consequences

Nexo keeps vendor-neutral OpenTelemetry as its only instrumentation, so LangSmith stays a
replaceable backend. Evaluation results move from JSON logs to LangSmith experiments.

[^langsmith]: LangSmith
