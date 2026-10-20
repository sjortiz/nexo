---
type: Architecture Decision
title: "ADR-0015: SigNoz as the default observability backend"
description: Self-host SigNoz as the single pane for traces, metrics, logs and alerts; Langfuse becomes an add-on for LLM evaluation; the Grafana LGTM add-on is removed.
tags: [adr, observability, signoz, opentelemetry]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T18:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: signoz
    resource: ../references/signoz.md
    title: SigNoz
  - id: langfuse
    resource: ../references/langfuse.md
    title: Langfuse
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
  - id: compose
    resource: ../../deploy/signoz/compose.yml
    title: Nexo's SigNoz compose file
---

# Context

[ADR-0014](adr-0014-langfuse-default-trace-backend.md) made Langfuse the default. In Nexo it was only a
trace viewer: its evaluation features (datasets, experiments, scores) are not wired to `nexo eval`. The
default pipeline dropped metrics and logs, so no single place showed traces, metrics, logs and alerts.[^collector]

# Decision

- **SigNoz is the default**, self-hosted by `make up`: APM and traces, service map, exceptions,
  metrics (including `nexo eval` scores), logs, dashboards, alerts and GenAI token and cost views, on
  native OpenTelemetry ingestion.[^signoz] The collector sends traces, metrics and logs to it, behind
  the same content stripping.
- **Langfuse becomes an add-on** for LLM evaluation and fine-tuning work (datasets, experiments, scores,
  LLM-as-judge, annotation queues): `make up LANGFUSE=1`.[^langfuse] Jaeger and LangSmith stay add-ons.
- **The Grafana LGTM add-on is removed**: SigNoz stores the same traces, metrics and logs.
- **Host and container metrics** (CPU, load, memory, disk and filesystem per host; CPU, memory,
  network and I/O per container) come from the collector's `host_metrics` and `docker_stats` receivers.
  The collector moves from 0.111.0 to 0.162.0, whose `docker_stats` works with Podman.
- **The compose file is pinned and generated once** with SigNoz's installer (foundryctl 0.3.0, docker
  compose casting), then renamed and pinned; Nexo does not run foundry.[^compose]

# Options considered

| Option | Against |
|---|---|
| Langfuse as the default (status quo) | Traces only; no metrics, logs, alerts or infrastructure view. |
| Both by default | Langfuse's stack (about 3 GB measured) on top of SigNoz's, before any model runs. |
| Grafana LGTM | A development image; no APM view or alerts in one product. |
| New Relic or Datadog | Accept OTLP, but are not open source or self-hosted. |

# Consequences

- Memory, measured on a 24 GB M5 Pro after an ingest: SigNoz's containers use 1.2-1.4 GB (ClickHouse
  0.9-1.1 GB), against 3.1 GB for the Langfuse stack. The default stack with the m5-pro-24 models is
  about 8.4 GB (10.2 GB before); with every add-on, 5.1 GB of containers and about 11.6 GB in all.
- With `LANGFUSE=1` there are two ClickHouse instances.
- SigNoz has no LLM evaluation features, which is why Langfuse stays as an add-on.
- SigNoz releases often: images are pinned, and an upgrade re-generates the compose file with foundryctl
  and diffs it against `deploy/signoz/`.
- The collector runs as root in its container to read the container engine's socket (with rootless
  Podman, the unprivileged user that owns it), with SELinux labeling off for that container.
- SigNoz is MIT outside `ee/` and `cmd/enterprise/`; its stats reporting is off.

[^signoz]: SigNoz
[^langfuse]: Langfuse
[^collector]: Collector configuration
[^compose]: Nexo's SigNoz compose file
