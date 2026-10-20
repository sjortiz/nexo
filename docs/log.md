# Bundle Update Log

## 2026-10-20
* **Update**: A `Makefile` covers setup, models, services, tests, the walkthrough and the site; CI runs its targets ([Testing](operations/testing.md)).

## 2026-10-19
* **Creation**: Step-by-step walkthrough ([page](https://sjortiz.github.io/nexo/walkthrough.html), `examples/walkthrough.ipynb`, `examples/walkthrough.py`), generated from one real run.
* **Update**: Profiles pin GGUF Ollama tags (MLX builds cannot do structured output); `nexo models --check` probes structured output for JSON roles ([How local setup works](operations/local-setup.md)).
* **Update**: vLLM services moved to `docker-compose.gpu.yml`; local runs no longer need GPU secrets for `compose up neo4j` ([Compose stack with vLLM](operations/local-stack.md)).
* **Creation**: [How local setup works](operations/local-setup.md): roles, one profile catalog with two readers, setting precedence, who listens where, `nexo models --check`. Playbooks link to it; stale "script stops if Ollama runs" gotcha corrected; "Local stack" retitled [Compose stack with vLLM](operations/local-stack.md).
* **Creation**: [MCP server](components/mcp-server.md), [Use from Claude Code](operations/claude-code.md) (skill + `.mcp.json`), [MCP reference](references/model-context-protocol.md), [ADR-0013](decisions/adr-0013-read-only-mcp-and-skill.md).
* **Update**: Sensitivity is now the least sensitive supporting source for entities and relationships, recomputed on every change ([data model](architecture/data-model.md), [ADR-0006](decisions/adr-0006-sensitivity-and-caller-clearance.md)).
* **Creation**: Core-stack references ([LangGraph](references/langgraph.md), [Strands Agents](references/strands-agents.md), [LangChain](references/langchain.md), [vLLM](references/vllm.md), [Neo4j](references/neo4j.md)) and the generated workflow diagram in the [orchestration graph](components/orchestration-graph.md).
* **Update**: Hybrid BM25 + vector seed search in the [chunk store](components/chunk-store.md) (`RETRIEVAL_MODE`); backend recommendations reworded open-source first.
* **Creation**: [Local on Linux](operations/local-linux.md): `gpu-`/`cpu-` profiles, Linux compose override, systemd-managed Ollama; suite verified on Linux.
* **Update**: Hardware profiles (`LOCAL_PROFILE`, `REMOTE_PROFILE`) and both run paths, Podman and native, in [Local on Apple Silicon](operations/local-apple-silicon.md); reasoning effort is now per role.
* **Creation**: [Local on Apple Silicon](operations/local-apple-silicon.md) playbook, [Ollama](references/ollama.md) reference and [ADR-0012](decisions/adr-0012-local-apple-silicon.md): per-role model endpoints across two Macs.
* **Update**: Added [entity resolution](components/entity-resolution.md), the [relation ontology](components/relation-ontology.md) and the lexical graph to the [data model](architecture/data-model.md), after reviewing five [knowledge-graph construction guides](references/).
* **Update**: Moved telemetry to the [OpenTelemetry GenAI conventions](references/opentelemetry-genai-conventions.md); documented in [Observability](operations/observability.md). Logging replaces CLI prints.
* **Creation**: [ADR-0007](decisions/adr-0007-entity-resolution.md) to [ADR-0011](decisions/adr-0011-langsmith-adoption.md); ADR-0011 (LangSmith) is proposed.
* **Creation**: Established the bundle: [architecture](architecture/), [components](components/), [operations](operations/), [decisions](decisions/) and [references](references/). All concepts start as `draft` and unverified.
* **Initialization**: Documented the code at the rename to Nexo (package `nexo`, CLI `nexo`).
