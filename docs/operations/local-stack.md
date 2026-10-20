---
type: Playbook
title: Compose stack with vLLM
description: Bring up Neo4j, vLLM on GPUs, tracing and the CLI with Docker Compose.
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
---

# Services

| Service | Profile | Notes |
|---|---|---|
| `neo4j` | default | `neo4j:5.26-community`, ports bound to `127.0.0.1`. |
| `otel-collector` | default | OTLP/HTTP on `127.0.0.1:4318`; strips content attributes and events; traces to Jaeger, metrics on `127.0.0.1:8889` (Prometheus format). |
| `jaeger` | default | UI on http://localhost:16686. |
| `vllm-inference-service` | `gpu` | Llama 3.3 70B, tensor-parallel 4 (4×80 GB GPUs), tool calling enabled. |
| `vllm-embedding-service` | `gpu` | BAAI/bge-m3 on 1 GPU. |
| `nexo` | `app` | The CLI image. |

Compose refuses to start without `VLLM_IMAGE_TAG`, `VLLM_API_KEY`, `HF_TOKEN`, `EMBEDDING_API_KEY` and
`NEO4J_PASSWORD`, so an unpinned image or an empty secret never ships silently.[^compose]

# Steps

1. `cp .env.example .env` and fill in the secrets; pin `VLLM_IMAGE_TAG` to a vLLM release.
2. `docker compose up -d neo4j otel-collector jaeger`
3. `docker compose --profile gpu up -d` (first start downloads ~140 GB of weights; the health check
   allows 15 minutes).
4. `pip install -e ".[dev]"` and run the CLI locally, or use the container:
   `docker compose --profile app run --rm nexo ingest /corpus/memo.md --doc-type memo`
   (`./corpus` is mounted read-only at `/corpus`).
5. `nexo ask "..."`, then `nexo review <thread_id> --approve --reviewer <you>`.

Without GPUs, point `VLLM_BASE_URL` at any OpenAI-compatible server with tool calling, and set
`EMBEDDING_PROVIDER=fake` for a dry run. On a Mac, use a hardware profile with Ollama instead:
[Local on Apple Silicon](local-apple-silicon.md).

# Tracing

The collector deletes GenAI content attributes (`gen_ai.input.messages`, `gen_ai.output.messages`,
`gen_ai.system_instructions`, tool arguments and results, `db.query.text`) and GenAI message events
before export, as a second line behind `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`.
To also send traces to LangSmith, uncomment the `otlphttp/langsmith` exporter
([ADR-0011](../decisions/adr-0011-langsmith-adoption.md)).[^otel]

[^compose]: Docker Compose stack
[^otel]: OTEL collector config
