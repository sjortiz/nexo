---
type: Playbook
title: Compose stack with vLLM
description: Bring up Neo4j, vLLM on GPUs, observability with SigNoz and the CLI with nexo setup (the linux-vllm preset).
resource: ../../examples/setups/linux-vllm/nexo.yaml
tags: [operations, deployment, docker, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-20T16:00:00Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: preset
    resource: ../../examples/setups/linux-vllm/nexo.yaml
    title: linux-vllm preset
  - id: components
    resource: ../../src/nexo/setup/components.py
    title: Components and their fragments
  - id: vllm
    resource: ../../deploy/compose/vllm.yaml
    title: vLLM compose fragment
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

`nexo setup` renders one compose file with the services `nexo.yaml` turns on, from fragments under
`deploy/` ([How local setup works](local-setup.md)).[^components]

| Service | Fragment | Notes |
|---|---|---|
| `neo4j` | `deploy/compose/neo4j.yaml` | `neo4j:5.26-community`, ports bound to `127.0.0.1`. |
| `otel-collector` | `deploy/compose/otel-collector.yaml` | OTLP/HTTP on `127.0.0.1:4318`; strips content attributes and events; traces, metrics and logs to SigNoz; metrics also on `127.0.0.1:8889` (Prometheus format). Host and container metrics with `addons.hostMetrics` (on by default). |
| `signoz` | `deploy/signoz/compose.yml` | SigNoz v0.146.0; UI on http://localhost:8080. |
| `signoz-ingester`, `signoz-clickhouse`, `signoz-keeper`, `signoz-postgres`, plus two one-shot jobs | `deploy/signoz/compose.yml` | Ingester v0.144.14, ClickHouse and Keeper 25.12.5, Postgres 16; schema migrator and UDF install. |
| `langfuse-web`, `langfuse-worker` | `deploy/langfuse/compose.yml` | Add-on (`addons.langfuse`, or `make up LANGFUSE=1`); Langfuse 4.64.0; UI on http://localhost:3000. |
| `langfuse-postgres`, `langfuse-clickhouse`, `langfuse-redis`, `langfuse-storage` | `deploy/langfuse/compose.yml` | Postgres 17, ClickHouse 25.12, Redis 7, SeaweedFS 4.48 (S3, on `127.0.0.1:9090`). |
| `jaeger` | `deploy/compose/jaeger.yaml` | Add-on (`addons.jaeger`, or `make up JAEGER=1`); UI on http://localhost:16686. |
| `vllm-inference-service` | `deploy/compose/vllm.yaml` | `models.provider: vllm`: Llama 3.3 70B, tensor-parallel 4 (4×80 GB GPUs), tool calling enabled; `127.0.0.1:8000`. |
| `vllm-embedding-service` | `deploy/compose/vllm.yaml` | BAAI/bge-m3 on 1 GPU; `127.0.0.1:8001`. |
| `nexo` | `deploy/compose/nexo.yaml` | The CLI image (`make app`); not started by `make up`. |

The vLLM image is pinned in its fragment and recorded in `nexo.lock`; `models.vllm` sets the model
and GPU count.[^vllm] Compose refuses to start without `NEO4J_PASSWORD`, the SigNoz secrets,
`VLLM_API_KEY`, `EMBEDDING_API_KEY` and `HF_TOKEN`; `make env` generates all but `HF_TOKEN`, and
`make check` names it when it is missing.

# Steps

1. `make env PRESET=linux-vllm`: `nexo.yaml` from the preset, `.env` with random secrets (existing
   values are kept). Add `HF_TOKEN` (the model is gated) to `.env`.[^preset]
2. `make check`, then `make up`: Neo4j, the collector, SigNoz and both vLLM services (the first start
   downloads ~140 GB of weights; the health check allows 15 minutes). Log in to
   http://localhost:8080 with `SIGNOZ_USER_EMAIL` (default `admin@nexo.local`) and `SIGNOZ_USER_PASSWORD`.
3. `uv sync` and run the CLI locally (`nexo.env` points it at `localhost:8000` and `:8001`), or use
   the container: `make app ARGS="ingest /corpus/memo.md --doc-type memo"` (`./corpus` is mounted
   read-only at `/corpus`).
4. `nexo ask "..."`, then `nexo review <thread_id> --approve --reviewer <you>`.

Without GPUs, use `provider: openai-compatible` with `models.roles` pointing at any OpenAI-compatible
server with tool calling, and set `EMBEDDING_PROVIDER=fake` in `.env` for a dry run. On a Mac, use a
hardware profile with Ollama instead: [Local on Apple Silicon](local-apple-silicon.md).

# Tracing

The collector deletes GenAI content attributes (`gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.system_instructions`, tool arguments and results, `db.query.text`) and GenAI message events
before export, as a second line behind `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`,
then sends traces, metrics and logs to SigNoz.[^otel] SigNoz publishes only its UI, on `127.0.0.1`, with
stats reporting off; the root user is provisioned from `.env`.[^signoz]

Add-ons, opt-in and combinable, each a pipeline in `deploy/otel-addons/` with the same processors,
merged into the rendered collector config ([ADR-0015](../decisions/adr-0015-signoz-default-observability.md)):

- Langfuse (evaluation): `addons.langfuse.enabled: true`, or `make up LANGFUSE=1` for one run.
  Langfuse publishes only its UI and storage, on `127.0.0.1`, with usage telemetry off; a headless setup creates
  the `nexo` project, its API keys and the login (`LANGFUSE_USER_EMAIL` / `LANGFUSE_USER_PASSWORD`).[^langfuse]
- Jaeger: `addons.jaeger.enabled: true`, or `make up JAEGER=1`.
- LangSmith: `LANGSMITH_API_KEY` in `.env` and `addons.langsmith` (`project`, default `nexo`), or
  `make up LANGSMITH=1`.

[^preset]: linux-vllm preset
[^components]: Components and their fragments
[^vllm]: vLLM compose fragment
[^otel]: OTEL collector config
[^signoz]: SigNoz compose file
[^langfuse]: Langfuse compose file
