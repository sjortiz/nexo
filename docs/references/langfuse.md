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
  - id: otel-experiments
    resource: https://langfuse.com/integrations/native/opentelemetry/experiments
    title: Ingest experiment spans with OpenTelemetry
  - id: api-reference
    resource: https://api.reference.langfuse.com
    title: Langfuse API reference
  - id: experiments
    resource: ../../src/nexo/experiments.py
    title: Nexo's Langfuse experiments
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
after stripping message content. It shows GenAI spans as generations with model and token usage, and
`nexo eval` runs as an experiment on a dataset of the golden set ([below](#experiments)).
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
(`~/.config/nexo/config` by default; ADR-0017).

# Read API

Langfuse v4 reads observations from `/api/public/v2/observations` (filter by `traceId`). The v3
endpoint `/api/public/traces` returns 404.[^api]

# Experiments

Langfuse v4 has no endpoint that links a trace to a dataset run: it builds experiments from
OpenTelemetry attributes.[^otel-experiments] `nexo eval` uses the public API only for the dataset and
the scores:[^experiments][^api-reference]

| Step | How |
|---|---|
| Dataset | `POST /api/public/v2/datasets` with the name; the same name updates it. |
| Items | `POST /api/public/dataset-items` with an `id` (unique in the project, 255 characters at most); the same id updates the item and returns a new `version`. |
| Experiment | Baggage on each case's root span: `langfuse.experiment.id`, `.name`, `.dataset.id`, `.metadata.*`. The root has no parent, so each case is its own trace. |
| Item | On the root and, through baggage, its children: `langfuse.experiment.item.id`, `.version` and `.root_observation_id` (the root's own span id). |
| Scores | `POST /api/public/scores` with `traceId` and `observationId` (the root span), `dataType: NUMERIC`; the same `id` overwrites a score. |

Verified on 4.64.0: one OTLP trace with these attributes became an experiment on the dataset with one
item, and a score posted on its root appeared on that item (`/api/public/experiment-items`, which
needs `fromStartTime`). Experiment ids, names and model names are the only values in baggage, which
can reach outbound request headers.

[^otel]: Langfuse OpenTelemetry integration
[^otel-experiments]: Ingest experiment spans with OpenTelemetry
[^api-reference]: Langfuse API reference
[^experiments]: Nexo's Langfuse experiments
[^compose]: Langfuse self-hosting with Docker Compose
[^headless]: Langfuse headless initialization
[^blob]: Langfuse blob storage
[^telemetry]: Langfuse usage telemetry
[^api]: Langfuse public API
[^local]: Nexo's Langfuse compose file
[^collector]: Collector configuration
