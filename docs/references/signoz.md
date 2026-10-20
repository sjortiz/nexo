---
type: Reference
title: SigNoz
description: Open-source observability platform, self-hosted as Nexo's default backend - OpenTelemetry ingestion, the self-hosted components, host and container metrics and how the compose file was generated.
resource: https://signoz.io/docs/
tags: [reference, observability, signoz, opentelemetry]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T18:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: docs
    resource: https://signoz.io/docs/
    title: SigNoz documentation
  - id: local
    resource: ../../deploy/signoz/compose.yml
    title: Nexo's SigNoz compose file
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
---

# Role in Nexo

The default observability backend: `make up` starts it next to Neo4j and the OpenTelemetry collector,
and the collector sends traces, metrics and logs to it after stripping message content. One UI covers
APM and traces, the service map, exceptions, metrics (including `nexo eval` scores), logs, dashboards,
alerts and GenAI token and cost views.[^docs] It has no LLM evaluation features; Langfuse is the add-on
for those ([ADR-0015](../decisions/adr-0015-signoz-default-observability.md)).

# Ingestion

The collector exports OTLP gRPC to the SigNoz ingester (`signoz-ingester`). The Prometheus endpoint on
`127.0.0.1:8889` remains.[^collector]

# Self-hosted components

SigNoz v0.146.0, from `deploy/signoz/compose.yml`:[^local]

| Service | Image | Role |
|---|---|---|
| `signoz` | `signoz/signoz` v0.146.0 | UI and API on http://localhost:8080 |
| `signoz-ingester` | `signoz/signoz-otel-collector` v0.144.14 | OTLP ingestion into ClickHouse |
| `signoz-clickhouse`, `signoz-keeper` | ClickHouse and ClickHouse Keeper 25.12.5 | Traces, metrics and logs |
| `signoz-postgres` | Postgres 16 | Users, dashboards, alerts |
| `signoz-migrator`, `signoz-clickhouse-scripts` | one-shot jobs | Schema migrations; histogramQuantile UDF install |

Only the UI is published, on `127.0.0.1:8080`. Log in with `SIGNOZ_USER_EMAIL` (default
`admin@nexo.local`) and `SIGNOZ_USER_PASSWORD` from `.env`; `make env` generates that password, the
JWT secret and the Postgres password. SigNoz's stats reporting is off. The license is MIT outside
`ee/` and `cmd/enterprise/`.

The compose file was generated once with SigNoz's installer (foundryctl 0.3.0, docker compose
casting), then pinned and renamed. Nexo does not run foundry; to upgrade, re-generate and diff.

# Host and container metrics

The collector's `host_metrics` receiver reads CPU, load, memory, paging and disk from the container's
`/proc` (which reports the host's) and root filesystem usage from a read-only `/hostfs` mount; rootless
Podman cannot read the host's mount table, so `root_path` is not used. `docker_stats` reads
per-container CPU, memory, network and I/O from the container engine's socket. `nexo setup` renders the
engine's socket path (Podman's rootless socket on macOS). The collector runs as root in its container,
with SELinux labeling off, to read it.

On macOS the host is the Podman VM, not the Mac, and Ollama runs outside it, so neither is covered.

[^docs]: SigNoz documentation
[^local]: Nexo's SigNoz compose file
[^collector]: Collector configuration
