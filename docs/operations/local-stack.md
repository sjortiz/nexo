---
type: Playbook
title: Compose stack with vLLM
description: Bring up Neo4j, vLLM on GPUs, observability with SigNoz and the CLI with Docker Compose.
resource: ../../docker-compose.yml
tags: [operations, deployment, docker, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: compose
    resource: ../../docker-compose.yml
    title: Docker Compose stack
  - id: otel
    resource: ../../deploy/otel-collector.yaml
    title: OTEL collector config
  - id: signoz
    resource: ../../deploy/signoz/compose.yml
    title: SigNoz compose file
  - id: langfuse
    resource: ../../deploy/langfuse/compose.yml
    title: Langfuse compose file
---

# Services

| Service | Where | Notes |
|---|---|---|
| `neo4j` | `docker-compose.yml` | `neo4j:5.26-community`, ports bound to `127.0.0.1`. |
| `otel-collector` | `docker-compose.yml` | OTLP/HTTP on `127.0.0.1:4318`; strips content attributes and events; traces, metrics and logs to SigNoz, plus host and container metrics; metrics also on `127.0.0.1:8889` (Prometheus format). |
| `signoz` | `deploy/signoz/compose.yml` (included) | SigNoz v0.146.0; UI on http://localhost:8080. |
| `signoz-ingester`, `signoz-clickhouse`, `signoz-keeper`, `signoz-postgres`, plus two one-shot jobs | `deploy/signoz/compose.yml` | Ingester v0.144.14, ClickHouse and Keeper 25.12.5, Postgres 16; schema migrator and UDF install. |
| `langfuse-web`, `langfuse-worker` | `deploy/langfuse/compose.yml`, profile `langfuse` | Add-on (`make up LANGFUSE=1`); Langfuse 4.64.0; UI on http://localhost:3000. |
| `langfuse-postgres`, `langfuse-clickhouse`, `langfuse-redis`, `langfuse-storage` | `deploy/langfuse/compose.yml`, profile `langfuse` | Postgres 17, ClickHouse 25.12, Redis 7, SeaweedFS 4.48 (S3, on `127.0.0.1:9090`). |
| `jaeger` | `docker-compose.yml`, profile `jaeger` | Add-on (`make up JAEGER=1`); UI on http://localhost:16686. |
| `vllm-inference-service` | `docker-compose.gpu.yml` | Llama 3.3 70B, tensor-parallel 4 (4×80 GB GPUs), tool calling enabled. |
| `vllm-embedding-service` | `docker-compose.gpu.yml` | BAAI/bge-m3 on 1 GPU. |
| `nexo` | `docker-compose.yml`, profile `app` | The CLI image. |

Compose refuses to start without `NEO4J_PASSWORD` and the SigNoz secrets (`make env` generates
them), and with the GPU file also without `VLLM_IMAGE_TAG`, `VLLM_API_KEY`, `HF_TOKEN` and
`EMBEDDING_API_KEY`, so an unpinned image or an empty secret never ships silently.[^compose] The vLLM services live in their own file because Compose checks required variables
for every service it loads; local runs with Ollama do not need them.

# Steps

1. `cp .env.example .env`, fill in the vLLM secrets and pin `VLLM_IMAGE_TAG` to a vLLM release; then
   `make env` fills `NEO4J_PASSWORD`, the SigNoz secrets and the Langfuse secrets with random values (existing values are kept).
2. `make up`, or `docker compose up -d neo4j otel-collector signoz`. Log in to
   http://localhost:8080 with `SIGNOZ_USER_EMAIL` (default `admin@nexo.local`) and `SIGNOZ_USER_PASSWORD`.
3. `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d` (first start downloads ~140 GB of weights; the health check
   allows 15 minutes).
4. `uv sync` and run the CLI locally, or use the container:
   `docker compose --profile app run --rm nexo ingest /corpus/memo.md --doc-type memo`
   (`./corpus` is mounted read-only at `/corpus`).
5. `nexo ask "..."`, then `nexo review <thread_id> --approve --reviewer <you>`.

Without GPUs, point `VLLM_BASE_URL` at any OpenAI-compatible server with tool calling, and set
`EMBEDDING_PROVIDER=fake` for a dry run. On a Mac, use a hardware profile with Ollama instead:
[Local on Apple Silicon](local-apple-silicon.md).

# Tracing

The collector deletes GenAI content attributes (`gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.system_instructions`, tool arguments and results, `db.query.text`) and GenAI message events
before export, as a second line behind `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`,
then sends traces, metrics and logs to SigNoz.[^otel] SigNoz publishes only its UI, on `127.0.0.1`, with
stats reporting off; the root user is provisioned from `.env`.[^signoz]

Add-ons, opt-in and combinable, each a pipeline in `deploy/otel-addons/` with the same processors
([ADR-0015](../decisions/adr-0015-signoz-default-observability.md)):

- Langfuse (evaluation): `make up LANGFUSE=1`, or `OTEL_LANGFUSE=langfuse docker compose --profile langfuse up -d neo4j otel-collector signoz langfuse-web langfuse-worker`.
  Langfuse publishes only its UI and storage, on `127.0.0.1`, with usage telemetry off; a headless setup creates
  the `nexo` project, its API keys and the login (`LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD`).[^langfuse]
- Jaeger: `make up JAEGER=1`, or `OTEL_JAEGER=jaeger docker compose up -d neo4j otel-collector signoz jaeger`.
- LangSmith: set `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` in `.env`, then `make up LANGSMITH=1`, or
  `OTEL_LANGSMITH=langsmith` before the same `docker compose up`.

[^compose]: Docker Compose stack
[^otel]: OTEL collector config
[^signoz]: SigNoz compose file
[^langfuse]: Langfuse compose file
