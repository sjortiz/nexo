---
type: Configuration Reference
title: Configuration reference
description: Every environment variable Nexo reads, with defaults and effects.
resource: ../../src/nexo/config.py
tags: [operations, configuration]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: config
    resource: ../../src/nexo/config.py
    title: Settings
  - id: env
    resource: ../../.env.example
    title: Environment template
---

Settings load from the environment, then the secrets provider for the generated secrets
(`NEO4J_PASSWORD`, `VLLM_API_KEY`, `EMBEDDING_API_KEY`; `~/.config/nexo/config` by default, read
in-process, see ADR-0017),
then `.nexo/render/nexo.env` (rendered from `nexo.yaml` by `nexo setup`), then `.env` (names are
case-insensitive). A test fails if a setting is missing from
this page.[^config] Template: `.env.example`.[^env]

# Schema

**Default chat model** (any OpenAI-compatible server: vLLM, Ollama, ...)

| Variable | Default | Effect |
|---|---|---|
| `VLLM_BASE_URL` | `http://vllm-inference-service:8000/v1` | Endpoint for every role that is not overridden below. |
| `VLLM_API_KEY` | `EMPTY` | Must match vLLM's `--api-key`. Secret. |
| `VLLM_MODEL` | `meta-llama/Llama-3.3-70B-Instruct` | Served model name. |
| `LLM_TIMEOUT_S` | `120` | Per-request timeout. |
| `LLM_MAX_RETRIES` | `3` | Client retries on connection errors, 408/409/429 and 5xx. |
| `AGENT_TEMPERATURE` | `0.1` | Agent sampling temperature. |
| `AGENT_MAX_TOKENS` | `2048` | Max output tokens per agent model call. |

**Per-role models** — each falls back to the `VLLM_*` value; use them to put roles on different models or
hosts ([Local on Apple Silicon](local-apple-silicon.md)).

| Variable | Default | Effect |
|---|---|---|
| `AGENT_BASE_URL` / `AGENT_API_KEY` / `AGENT_MODEL` | unset | Strands investigator (multi-turn tool calls). |
| `EXTRACTION_BASE_URL` / `EXTRACTION_API_KEY` / `EXTRACTION_MODEL` | unset | Triple extraction and entity resolution. |
| `ROUTER_BASE_URL` / `ROUTER_API_KEY` / `ROUTER_MODEL` | unset | Intake validation. |
| `EXTRACTION_REASONING_EFFORT` / `ROUTER_REASONING_EFFORT` / `JUDGE_REASONING_EFFORT` | unset | Sent as `reasoning_effort` to that JSON role only; `none` for thinking models. Unset = not sent. The agent has no such setting. |

**Local hardware profiles** — pick models by Mac instead of setting them one by one
([Local on Apple Silicon](local-apple-silicon.md)). Explicit role settings above win.

| Variable | Default | Effect |
|---|---|---|
| `LOCAL_PROFILE` | unset | Profile of the machine running Nexo: `m1-max-64`, `m5-pro-24` (Apple), `gpu-24` (Linux GPU VRAM), `cpu-64` (RAM). Alone: this machine serves every role. Set by `nexo.yaml` (`models.local.profile`). |
| `LOCAL_BASE_URL` | `http://localhost:11434/v1` | This machine's Ollama (from a container: `host.containers.internal` on macOS, `127.0.0.1` with host networking on Linux). |
| `REMOTE_PROFILE` | unset | Profile of a second machine (Mac or Linux GPU box) that serves the agent and extraction models. Requires `LOCAL_PROFILE`. Set by `nexo.yaml` (`models.remote.profile`). |
| `REMOTE_BASE_URL` | `http://localhost:11435/v1` | The second Mac's Ollama, usually through an SSH tunnel. |

**Embeddings**

| Variable | Default | Effect |
|---|---|---|
| `EMBEDDING_PROVIDER` | `openai_compatible` | `fake` gives deterministic offline vectors (tests, dev). |
| `EMBEDDING_BASE_URL` | `http://vllm-embedding-service:8000/v1` | Separate pooling server. |
| `EMBEDDING_API_KEY` | `EMPTY` | Secret. |
| `EMBEDDING_MODEL` | `BAAI/bge-m3` | Embedding model. |
| `EMBEDDING_DIMENSIONS` | `1024` | Used by the fake provider. |

**Neo4j**

| Variable | Default | Effect |
|---|---|---|
| `NEO4J_URI` | `neo4j://neo4j:7687` | Bolt/neo4j URI. |
| `NEO4J_DATABASE` | `neo4j` | Database name. |
| `NEO4J_USER` / `NEO4J_PASSWORD` | `neo4j` / empty | Writer account (ingestion). Secret. |
| `NEO4J_READ_USER` / `NEO4J_READ_PASSWORD` | unset | Reader-role account for the agent tool; falls back to the writer account. |
| `NEO4J_QUERY_TIMEOUT_S` | `15` | Per agent query. |
| `NEO4J_MAX_ROWS` | `50` | Row cap per agent query (1–1000). |

**Storage and ingestion**

| Variable | Default | Effect |
|---|---|---|
| `VECTOR_STORE_PATH` | `.data/vector_store.json` | Local chunk store file. |
| `RETRIEVAL_MODE` | `hybrid` | Seed search: `hybrid` (BM25 + vector, Reciprocal Rank Fusion), `vector` or `keyword` (BM25 only). |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | `1200` / `150` | Characters per chunk and overlap inside a section. |
| `EXTRACTION_ATTEMPTS` | `3` | Validation retries per chunk. |
| `EXTRACTION_CONCURRENCY` | `8` | Parallel extraction calls. |
| `REQUIRE_GROUNDED_EVIDENCE` | `true` | Drop triples whose evidence quote is not in the chunk. |
| `EXTRACTION_CONTEXT_CHARS` | `500` | Tail of the previous chunk shown to the extractor (context only); 0 disables. |
| `ONTOLOGY_PATH` | unset | [Relation ontology](../components/relation-ontology.md) JSON; unset keeps free-form predicates. |
| `GLOSSARY_PATH` | unset | JSON object of acronym → expansion. |

**Entity resolution**

| Variable | Default | Effect |
|---|---|---|
| `ENTITY_RESOLUTION` | `llm` | `off`, `deterministic` or `llm` ([entity resolution](../components/entity-resolution.md)). |
| `RESOLUTION_MAX_CANDIDATES` | `25` | Candidates offered to the model per mention. |
| `RESOLUTION_BATCH_SIZE` | `30` | Mentions per model call. |

**Sensitive data**

| Variable | Default | Effect |
|---|---|---|
| `SENSITIVE_DATA_PROVIDER` | `regex` | `none`, `regex` or `comprehend_medical`. |
| `SENSITIVE_DATA_POLICY` | `tag` | `tag` or `redact`. |
| `AWS_REGION` | unset | Required for `comprehend_medical`. |
| `DEFAULT_CLEARANCE` | `internal` | Clearance when the caller does not pass one. |
| `MCP_CLEARANCE` | unset (= `DEFAULT_CLEARANCE`) | Clearance of the `nexo mcp` server: one per server, since MCP over stdio has no per-user identity. |

**Investigator agent**

| Variable | Default | Effect |
|---|---|---|
| `AGENT_MAX_TURNS` | `12` | Strands loop turns. |
| `AGENT_MAX_TOTAL_TOKENS` | `120000` | Cumulative token cap. |
| `AGENT_MAX_TOOL_CALLS` | `16` | Tool calls per investigation. |
| `AGENT_TIMEOUT_S` | `300` | Wall-clock cap. |
| `AGENT_MAX_CHUNKS_PER_CALL` | `10` | Chunks per `fetch_vector_chunks` call. |
| `SEED_CHUNKS` | `5` | Vector seeds for `vector`/`hybrid` strategies (0 disables). |
| `TOOL_RESULT_MAX_CHARS` | `12000` | Max serialized tool output. |

**Orchestration**

| Variable | Default | Effect |
|---|---|---|
| `MAX_QUERY_CHARS` | `2000` | Router's deterministic length gate. |
| `CHECKPOINT_BACKEND` | `sqlite` | `memory` (tests only), `sqlite` (single node) or `postgres`. |
| `CHECKPOINT_SQLITE_PATH` | `.data/checkpoints.sqlite` | SQLite file. |
| `CHECKPOINT_POSTGRES_URI` | unset | Required for `postgres` (extra `postgres`). Secret. |

**Evaluation**

| Variable | Default | Effect |
|---|---|---|
| `JUDGE_BASE_URL` / `JUDGE_API_KEY` | unset (fall back to `VLLM_*`) | Judge endpoint. |
| `JUDGE_MODEL` | unset | Enables LLM-as-a-judge in `nexo eval`. |

**Observability**

| Variable | Default | Effect |
|---|---|---|
| `OTEL_SERVICE_NAME` | `nexo` | `service.name` resource attribute. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP base URL; unset disables export. |
| `OTEL_CONSOLE_EXPORT` | `false` | Print spans to stdout. |
| `OTEL_LOGS_EXPORTER` | `none` | `otlp` also sends log records to the collector, with trace ids; `result` payloads only with content capture on. Stored by SigNoz; `nexo.yaml` sets it (`telemetry.logs`, default `otlp`). |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | `no_content` | `span_only` / `span_and_event` put prompts, outputs and agent Cypher on spans (may carry PII). Standard GenAI variable. |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | derived | Strands conventions and redaction; derived from content capture unless set. |
| `OTEL_INSTRUMENT_OPENAI` | `true` | Official GenAI instrumentation of the openai SDK (one `chat {model}` span per vLLM call). |
| `DEPLOYMENT_ENVIRONMENT` | `dev` | `deployment.environment.name` attribute. |

**Logging**

| Variable | Default | Effect |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Root log level. |
| `LOG_FORMAT` | `text` | `text` or `json` (one object per line, with `trace_id`, `span_id` and `result`). |

[^config]: Settings
[^env]: Environment template
