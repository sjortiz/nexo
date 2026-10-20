---
type: Architecture Decision
title: "ADR-0014: Langfuse as the default trace backend"
description: Self-host Langfuse as the default trace backend; Jaeger and LangSmith become opt-in collector add-ons; SeaweedFS instead of the archived MinIO.
tags: [adr, observability, langfuse, opentelemetry]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T12:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: langfuse
    resource: ../references/langfuse.md
    title: Langfuse
  - id: langsmith
    resource: ../references/langsmith.md
    title: LangSmith
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
---

# Context

Jaeger showed Nexo's spans but had no view of LLM calls (generations, token usage).
[ADR-0011](adr-0011-langsmith-adoption.md) proposed LangSmith as a second backend, but self-hosting it
needs an Enterprise plan, and the data may include PHI.[^langsmith]

# Decision

- **Langfuse is the default trace backend**, self-hosted by `make up`: open-source core, self-hosting
  on every tier, and native OpenTelemetry ingestion, so Nexo's instrumentation does not change.[^langfuse]
- **Jaeger and LangSmith are opt-in add-ons** (`make up JAEGER=1`, `LANGSMITH=1`). Each is a collector
  config fragment in `deploy/otel-addons/` with its own pipeline (`traces/jaeger`, `traces/langsmith`)
  and the same content-stripping processors.[^collector]
- **SeaweedFS** provides Langfuse's S3 storage; MinIO's community repository was archived in April 2026.
- **Amended 2026-10-20:** a third add-on, Grafana LGTM (`make up LGTM=1`), stores traces, metrics and
  logs, which Langfuse does not take. Log export from Nexo is opt-in (`OTEL_LOGS_EXPORTER=otlp`); the
  base pipeline drops logs. An all-in-one platform as the default (e.g. SigNoz) was weighed: it would
  cover operations, but not Langfuse's datasets, experiments, scores and annotation queues.

# Options considered

| Option | Against |
|---|---|
| LangSmith as the default | Self-hosting only on Enterprise; Cloud would receive traces that may carry PHI. |
| Jaeger only | No LLM view: generations, models and token usage are plain spans. |
| Drop Jaeger | Kept as an opt-in add-on at the user's choice. |
| MinIO for storage | Community repository archived in April 2026. |

# Consequences

`make up` runs six more containers (Langfuse web and worker, Postgres, ClickHouse, Redis, SeaweedFS),
and `make env` generates their secrets. Message content stays out of every backend. ADR-0011
is superseded for the trace backend; LangSmith remains an option for evaluation and Studio.

[^langfuse]: Langfuse
[^langsmith]: LangSmith
[^collector]: Collector configuration
