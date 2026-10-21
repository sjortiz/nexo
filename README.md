# Nexo

Graph RAG investigation service: LangGraph orchestration, a Strands Agents micro-agent, a self-hosted vLLM
model (Llama 3.3 70B), Neo4j and a vector chunk store, traced end to end with OpenTelemetry.
Overview: [sjortiz.github.io/nexo](https://sjortiz.github.io/nexo/). Step by step, with real outputs:
[the walkthrough](https://sjortiz.github.io/nexo/walkthrough.html), also as a
[Jupyter notebook](examples/walkthrough.ipynb).

```
            ┌─────────────── LangGraph StateGraph (checkpointed) ──────────────┐
request ──► │ router ──► investigator ─────────────► compliance ──interrupt──► │──► reviewer (or policy) approves ──► answer
            │   │          │ @task: Strands Agent        (pure checks)         │
            │   ▼          │   ├─ query_knowledge_graph ──► Neo4j (READ tx)    │
            │  END         │   └─ fetch_vector_chunks  ──► chunk store         │
            └──────────────┴──────────────── vLLM (OpenAI API) ────────────────┘
ingest: document ─► heading-aware chunks ─► embeddings ─► chunk store
                                         └► vLLM triple extraction ─► evidence check ─► ontology ─► entity resolution
                                            ─► Neo4j: Document/Chunk lexical graph + entities (chunk_id on every node/edge)
```

The LangGraph workflow, generated from the compiled graph (`nexo graph`):

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	router(router)
	investigator(investigator)
	compliance(compliance)
	__end__([<p>__end__</p>]):::last
	__start__ --> router;
	investigator --> compliance;
	router -. &nbsp;rejected&nbsp; .-> __end__;
	router -.-> investigator;
	compliance --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| Module | Code |
| --- | --- |
| A. Storage | `storage/` — `ingest.py`, `chunking.py`, `schemas.py`, `ontology.py`, `resolution.py`, `graph_store.py`, `vector_store.py`, `sensitive_data.py` |
| B. Micro-agent | `agents/` — `investigator.py` (Strands Agent), `tools.py` (the agent's tools) |
| C. Orchestration | `orchestration/` — `graph.py` (3 nodes), `state.py`, `compliance.py` |
| Wiring | `service.py`, `cli.py`, `telemetry.py`, `logging_setup.py`, `llm.py`, `evaluation.py` |

Full documentation lives in [`docs/`](docs/index.md) as an [Open Knowledge Format](docs/references/open-knowledge-format.md)
bundle: architecture, components, operations, decisions and references.

## Run

Pick your path first:

| Machine | Preset | Guide |
| --- | --- | --- |
| Apple Silicon Mac, services in Podman | `mac-single` (`mac-two-machines` for two Macs) | [Local on Apple Silicon](docs/operations/local-apple-silicon.md) |
| Apple Silicon Mac, no containers | `mac-native` | [Native path](docs/operations/local-apple-silicon.md#native-path) |
| Linux, GPU or CPU, Ollama | `linux-gpu`, `linux-cpu`, `linux-native` | [Local on Linux](docs/operations/local-linux.md) |
| Linux GPU server, vLLM (4x80 GB) | `linux-vllm` | below, and [Compose stack with vLLM](docs/operations/local-stack.md) |

`make env PRESET=...` writes `nexo.yaml` with this machine's hardware profile; `make up` starts it;
`make check` says what is missing. The GPU-server flow:

```bash
uv sync && source .venv/bin/activate                  # Python 3.12 + deps from uv.lock
make env PRESET=linux-vllm                             # nexo.yaml + secrets in ~/.config/nexo; HF_TOKEN in .env
make up                                                # Neo4j, collector, SigNoz, vLLM (4x80GB GPUs)
nexo ingest corpus/memo.md --doc-type memo --effective-date 2025-06-30
nexo ask "How is Acme Holdings connected to Gamma Clinics?"   # answer, or thread_id + review packet on findings
nexo review <thread_id> --approve --reviewer you@corp       # only for a paused thread
```

Traces, metrics and logs: http://localhost:8080 (SigNoz; log in with `SIGNOZ_USER_EMAIL` / `SIGNOZ_USER_PASSWORD` from
`~/.config/nexo/config`); raw metrics: http://localhost:8889/metrics. Telemetry follows the
[OpenTelemetry GenAI conventions](docs/references/opentelemetry-genai-conventions.md) with message content off by default
([Observability](docs/operations/observability.md)). Logs go to stderr with trace ids (`LOG_FORMAT=json` for log
pipelines); stdout carries only each command's JSON result. Relation ontology example: `deploy/ontology.example.json`. Neo4j Enterprise least-privilege reader:
`deploy/neo4j-rbac.cypher`.

## Use from Claude Code (MCP)

`nexo mcp` serves the knowledge graph as read-only MCP tools (index, hybrid search, chunks, entities,
neighbors, read-only Cypher) with the same clearance and Cypher safeguards as the agent. The repository
ships `.mcp.json` and a `nexo-knowledge` skill, so Claude Code in this directory can answer questions from
your documents with a citation per claim: [Use from Claude Code](docs/operations/claude-code.md).

## Run locally (macOS and Linux)

Models run natively through Ollama, chosen by a hardware profile: `m1-max-64` / `m5-pro-24` on Apple
Silicon, `gpu-24` (VRAM) or `cpu-64` on Linux. One file, `nexo.yaml`, describes the setup (models,
add-ons, the container engine), foundry-style; `make` runs it through `nexo setup`
([ADR-0016](docs/decisions/adr-0016-declarative-setup-file.md)).

```bash
# One Mac
make env PRESET=mac-single                   # nexo.yaml (profile detected; PROFILE= to override) + secrets
make up                                      # Ollama's models, Neo4j, collector, SigNoz
make check                                   # preflight + each role's model and server

# One Mac, no containers: Homebrew's Neo4j, Ollama and Nexo from the .venv
make env PRESET=mac-native && make neo4j-init && brew services start neo4j
make up && make check

# Two Macs: the remote Mac serves agent + extraction, this one router/embeddings/judge + Nexo
deploy/local/ollama-setup.sh m1-max-64 heavy  # on the M1 Max
make env PRESET=mac-two-machines              # on the M5 Pro
ssh -N -L 11435:127.0.0.1:11434 you@m1-max.local
make up && make check

NEXO_LIVE=1 pytest -m live -v                 # exercise the real models
```

Presets: `mac-single`, `mac-two-machines`, `mac-native`, `linux-cpu`, `linux-gpu`, `linux-native`, `linux-vllm`
(`examples/setups/`). `docker-compose.yml` is generated from `mac-single` (`make compose-file`) for
plain `podman compose up -d`.

Which preset, models and add-ons to pick, with memory per combination:
[Choosing a setup](docs/operations/decision-guide.md). How it fits together (roles, the profile
catalog, ports): [How local setup works](docs/operations/local-setup.md).
Step by step: [Local on Apple Silicon](docs/operations/local-apple-silicon.md) and
[Local on Linux](docs/operations/local-linux.md) (`nexo setup` puts the Nexo container on the host
network there).

| Platform | Status |
| --- | --- |
| macOS (Apple Silicon) | Test suite passes; Podman and native paths verified up to the model server (`target.mode: native` is covered by tests, not yet run end to end). |
| Linux | Test suite passes (verified on aarch64); Podman path uses host networking; vLLM GPU passthrough untested here. |
| Windows | Not tested; WSL2 with the Linux instructions is the likely path. |
| Neo4j 5.26 (Community) | Supported; the compose and CI default. Chunk search uses the `db.index.*.queryNodes` procedures. |
| Neo4j 2026.09+ (Community) | Supported; integration tests pass on 2026.09 and 2026.10. Chunk search uses the `SEARCH` clause ([Chunk store](docs/components/chunk-store.md#neo4j-versions)). |

## Test

```bash
make test          # offline: fakes only at the model HTTP edge
make test-neo4j    # integration tests on a throwaway Neo4j (127.0.0.1:7688)
make test-live     # the real models, once they run
```

`make` lists every task (setup, models, services, ingest/ask, tests, the walkthrough and the site).

## Production notes

- `LocalChunkStore` is single-node (hybrid BM25 + vector search in-process). Implement `ChunkStore` on PostgreSQL +
  pgvector (exact id lookup, SQL metadata filters, ACID) or OpenSearch (high QPS); both open source.
- Human review pauses must survive restarts: use `CHECKPOINT_BACKEND=postgres` with more than one replica.
- `SENSITIVE_DATA_PROVIDER=comprehend_medical` sends chunk text to Amazon Comprehend Medical; enable only where that
  flow is approved. Entity `proposed_codes` are LLM output until verified against a terminology service.

## Observability

Nexo emits vendor-neutral OpenTelemetry. The collector strips message content and sends traces to a
self-hosted [SigNoz](docs/references/signoz.md) (http://localhost:8080): traces, service map, metrics,
logs, dashboards and alerts in one place, plus host and container metrics
([ADR-0015](docs/decisions/adr-0015-signoz-default-observability.md)).
Opt-in add-ons, each its own collector pipeline behind the same content stripping:

| Add-on | Start | Notes |
| --- | --- | --- |
| Langfuse | `make up LANGFUSE=1` | LLM evaluation and fine-tuning work (datasets, experiments, scores); UI on http://localhost:3000. |
| Jaeger | `make up JAEGER=1` | Span UI on http://localhost:16686. |
| LangSmith | `make up LANGSMITH=1` | Needs `LANGSMITH_API_KEY` in `.env`; the project is `addons.langsmith.project`. |

Add-ons can be combined. `make up LANGFUSE=1` turns one on for one run; `addons:` in `nexo.yaml`
keeps it on. Which to use when: [Choosing a setup](docs/operations/decision-guide.md#4-which-observability-to-turn-on).

LangSmith also offers Studio for local graph debugging (`langgraph dev`) and evaluation tooling
(datasets, experiments, `openevals` judges); see [the reference](docs/references/langsmith.md). No PHI
in LangSmith until a BAA is signed or LangSmith is self-hosted. Leave `LANGSMITH_TRACING` unset in the
app: the collector exports, so app-side tracing would duplicate traces.

## Sources

The design follows these sources; each has a reference page in [`docs/references/`](docs/references/index.md) saying
what was adopted, deferred or changed.

Core stack:
- [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) - orchestration
- [Strands Agents](https://strandsagents.com/docs/) - the investigator agent
- [LangChain](https://docs.langchain.com/oss/python/langchain/overview) - model clients, splitters, vector index
- [vLLM](https://docs.vllm.ai/en/latest/) - GPU inference
- [Neo4j](https://neo4j.com/docs/python-manual/current/) - graph database and driver

Knowledge graph construction:
- [Claude Cookbook: knowledge graph guide](https://platform.claude.com/cookbook/capabilities-knowledge-graph-guide)
- [Memgraph: knowledge graph creation](https://memgraph.com/docs/ai-ecosystem/graph-rag/knowledge-graph-creation)
- [LangChain: constructing knowledge graphs from text using OpenAI functions](https://www.langchain.com/blog/constructing-knowledge-graphs-from-text-using-openai-functions)
- [Neo4j LLM Graph Builder](https://neo4j.com/labs/genai-ecosystem/llm-graph-builder/)
- [Neo4j: how to build a knowledge graph](https://neo4j.com/blog/knowledge-graph/how-to-build-knowledge-graph/)

RAG on AWS:
- [Retrieval Augmented Generation for healthcare use cases](https://docs.aws.amazon.com/prescriptive-guidance/latest/rag-healthcare-use-cases/introduction.html)
- [Choosing an AWS vector database for RAG use cases](https://docs.aws.amazon.com/prescriptive-guidance/latest/choosing-an-aws-vector-database-for-rag-use-cases/introduction.html)
- [Writing best practices to optimize RAG applications](https://docs.aws.amazon.com/prescriptive-guidance/latest/writing-best-practices-rag/introduction.html)

Observability, evaluation and documentation:
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai) and
  [OpenTelemetry Python GenAI instrumentations](https://github.com/open-telemetry/opentelemetry-python-genai)
- [SigNoz](https://signoz.io/docs/) - default observability backend
- [Langfuse self-hosting](https://langfuse.com/self-hosting) - evaluation add-on, with
  [SeaweedFS](https://github.com/seaweedfs/seaweedfs) for its S3 storage
- [LangSmith documentation](https://docs.langchain.com/langsmith)
- [Model Context Protocol](https://modelcontextprotocol.io/) and its [Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [Ollama](https://github.com/ollama/ollama) and its [model library](https://ollama.com/library)
- [Open Knowledge Format specification](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)

## License

[MIT](LICENSE).
