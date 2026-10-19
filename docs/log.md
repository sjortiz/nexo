# Bundle Update Log

## 2026-10-19
* **Update**: Hardware profiles (`LOCAL_PROFILE`, `REMOTE_PROFILE`) and both run paths, Podman and native, in [Local on Apple Silicon](operations/local-apple-silicon.md); reasoning effort is now per role.
* **Creation**: [Local on Apple Silicon](operations/local-apple-silicon.md) playbook, [Ollama](references/ollama.md) reference and [ADR-0012](decisions/adr-0012-local-apple-silicon.md): per-role model endpoints across two Macs.
* **Update**: Added [entity resolution](components/entity-resolution.md), the [relation ontology](components/relation-ontology.md) and the lexical graph to the [data model](architecture/data-model.md), after reviewing five [knowledge-graph construction guides](references/).
* **Update**: Moved telemetry to the [OpenTelemetry GenAI conventions](references/opentelemetry-genai-conventions.md); documented in [Observability](operations/observability.md). Logging replaces CLI prints.
* **Creation**: [ADR-0007](decisions/adr-0007-entity-resolution.md) to [ADR-0011](decisions/adr-0011-langsmith-adoption.md); ADR-0011 (LangSmith) is proposed.
* **Creation**: Established the bundle: [architecture](architecture/), [components](components/), [operations](operations/), [decisions](decisions/) and [references](references/). All concepts start as `draft` and unverified.
* **Initialization**: Documented the code at the rename to Nexo (package `nexo`, CLI `nexo`).
