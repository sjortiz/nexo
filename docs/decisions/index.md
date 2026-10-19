# Architecture decisions

* [ADR-0001: Run the Strands agent inside a LangGraph @task](adr-0001-strands-agent-inside-langgraph-task.md) - The fixed workflow lives in LangGraph; open-ended reasoning lives in one Strands agent called from a checkpointed task.
* [ADR-0002: Read-only Cypher through three independent layers](adr-0002-read-only-defense-in-depth.md) - Agent-authored Cypher is constrained by a guard, a READ transaction and a database role.
* [ADR-0003: Durable checkpoints for human review](adr-0003-durable-checkpoints-for-review.md) - Human review uses interrupt/resume backed by SQLite or Postgres.
* [ADR-0004: Chunk store behind an interface; pgvector for production](adr-0004-chunk-store-interface-and-pgvector.md) - A local store ships now; Aurora/RDS pgvector is the production target.
* [ADR-0005: Guided decoding plus evidence grounding for extraction](adr-0005-guided-decoding-and-evidence-grounding.md) - Guided JSON, Pydantic feedback retries, and quotes that must appear in the chunk.
* [ADR-0006: Sensitivity classification and caller clearance](adr-0006-sensitivity-and-caller-clearance.md) - The caller's clearance, not the model, decides what evidence the agent can see.
* [ADR-0007: Conservative entity resolution with recorded merges](adr-0007-entity-resolution.md) - Key, alias, then a model arbiter over blocked candidates; merges auditable and reversible.
* [ADR-0008: Relation ontology and a lexical graph in Neo4j](adr-0008-ontology-and-lexical-graph.md) - Canonical relations with a RELATED_TO fallback; Document/Chunk structure in the graph.
* [ADR-0009: OpenTelemetry GenAI conventions, official instrumentation, content off](adr-0009-opentelemetry-genai-conventions.md) - Standard spans and metrics; no message content in telemetry by default.
* [ADR-0010: Deferred graph enrichment](adr-0010-deferred-graph-enrichment.md) - Techniques from the guides postponed, with the trigger for each.
* [ADR-0011: Where LangSmith fits](adr-0011-langsmith-adoption.md) - Proposed: trace backend and evaluation platform; workflow, review gate and prompts stay in Nexo.
* [ADR-0012: Ollama for local runs, with model roles split across Macs](adr-0012-local-apple-silicon.md) - Native models on Apple Silicon; each role can point at its own host and model.
