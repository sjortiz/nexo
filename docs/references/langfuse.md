---
type: Reference
title: Langfuse
description: Open-source LLM observability platform, self-hosted as an opt-in add-on for LLM evaluation - OpenTelemetry ingestion, the self-hosted components and the v4 read API.
resource: https://langfuse.com/self-hosting
tags: [reference, observability, langfuse, opentelemetry]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T12:00:00Z }
stale_after: 2027-01-20T00:00:00Z
sources:
  - id: otel
    resource: https://langfuse.com/integrations/native/opentelemetry
    title: Langfuse OpenTelemetry integration
  - id: compose
    resource: https://langfuse.com/self-hosting/deployment/docker-compose
    title: Langfuse self-hosting with Docker Compose
  - id: headless
    resource: https://langfuse.com/self-hosting/administration/headless-initialization
    title: Langfuse headless initialization
  - id: blob
    resource: https://langfuse.com/self-hosting/deployment/infrastructure/blobstorage
    title: Langfuse blob storage
  - id: telemetry
    resource: https://langfuse.com/self-hosting/security/telemetry
    title: Langfuse usage telemetry
  - id: api
    resource: https://langfuse.com/docs/api-and-data-platform/features/public-api
    title: Langfuse public API
  - id: local
    resource: ../../deploy/langfuse/compose.yml
    title: Nexo's Langfuse compose file
  - id: collector
    resource: ../../deploy/otel-collector.yaml
    title: Collector configuration
---

# Role in Nexo

An opt-in add-on for LLM evaluation and fine-tuning work (datasets, experiments, scores, LLM-as-judge,
annotation queues): `make up LANGFUSE=1` starts it, and the collector then also sends every trace to it
after stripping message content. It shows GenAI spans as generations with model and token usage.
SigNoz is the default backend ([ADR-0015](../decisions/adr-0015-signoz-default-observability.md);
[ADR-0014](../decisions/adr-0014-langfuse-default-trace-backend.md) made Langfuse the default before).

# OpenTelemetry ingestion

| Item | Value |
|---|---|
| Endpoint | `http://langfuse-web:3000/api/public/otel` (OTLP/HTTP; the exporter adds `/v1/traces`)[^otel] |
| Authentication | Basic auth with the project's public and secret keys[^otel] |
| Header | `x-langfuse-ingestion-version: 4`; without it, traces can take minutes to appear[^collector] |

Verified locally: one ingest became one trace with SPAN observations (`ingest.document`,
`ingest.extract_triples`, `find_candidates`, `replace_source`), GENERATION observations
(`chat qwen3.5:9b-q4_K_M`, with token usage) and an EMBEDDING observation (`bge-m3`); no message
content was exported.

# Self-hosted components

Version 4.64.0, from `deploy/langfuse/compose.yml`, based on Langfuse's own Compose file:[^compose][^local]

| Service | Image | Role |
|---|---|---|
| `langfuse-web` | `ghcr.io/langfuse/langfuse` | UI and API on http://localhost:3000 |
| `langfuse-worker` | `ghcr.io/langfuse/langfuse-worker` | Background ingestion |
| `langfuse-postgres` | Postgres 17 | Users, projects, keys |
| `langfuse-clickhouse` | ClickHouse 25.12 | Traces and observations |
| `langfuse-redis` | Redis 7 | Queue and cache |
| `langfuse-storage` | SeaweedFS 4.48 | S3-compatible event and media storage, on `127.0.0.1:9090`[^blob] |

Only the UI and storage are published, on `127.0.0.1`. SeaweedFS replaces MinIO, whose community
repository was archived in April 2026. Usage telemetry to Langfuse is off
(`TELEMETRY_ENABLED=false`).[^telemetry]

Headless initialization creates an organization, a `nexo` project with the API keys the collector uses,
and an admin login (`LANGFUSE_USER_EMAIL`, default `admin@nexo.local`, and
`LANGFUSE_USER_PASSWORD`).[^headless] `make env` generates every secret in the secrets provider
(the keychain by default; ADR-0017).

# Read API

Langfuse v4 reads observations from `/api/public/v2/observations` (filter by `traceId`). The v3
endpoint `/api/public/traces` returns 404.[^api]

[^otel]: Langfuse OpenTelemetry integration
[^compose]: Langfuse self-hosting with Docker Compose
[^headless]: Langfuse headless initialization
[^blob]: Langfuse blob storage
[^telemetry]: Langfuse usage telemetry
[^api]: Langfuse public API
[^local]: Nexo's Langfuse compose file
[^collector]: Collector configuration
