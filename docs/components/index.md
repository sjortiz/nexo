# Storage (Module A)

* [Ingestion pipeline](ingestion-pipeline.md) - Document to chunks, vectors and graph triples, keeping every chunk_id resolvable.
* [Structure-aware chunking](chunking.md) - Heading-aware splits, table flattening, glossary notes and document lint.
* [Triple extraction](triple-extraction.md) - Guided-JSON LLM extraction of entity-relation-entity triples with evidence grounding.
* [Graph store](graph-store.md) - Neo4j access: atomic document replacement and read-only agent queries.
* [Relation ontology](relation-ontology.md) - Canonical relations with synonyms, inverses and type constraints; RELATED_TO fallback.
* [Entity resolution](entity-resolution.md) - Exact, alias and model-arbitrated merging of mentions into canonical entities.
* [Chunk store](chunk-store.md) - Get-by-id, filtered similarity search and delete-by-document for chunk text.
* [Sensitive data detection](sensitive-data-detection.md) - PII/PHI classification, redaction and caller clearance.

# Micro-agent (Module B)

* [Investigator agent](investigator-agent.md) - The Strands agent that traces relationships autonomously.
* [query_knowledge_graph](query-knowledge-graph.md) - Agent tool: read-only Cypher against Neo4j.
* [find_paths and entity_neighbours](graph-lookup-tools.md) - Agent tools: entity paths and neighbours without Cypher.
* [fetch_vector_chunks](fetch-vector-chunks.md) - Agent tool: exact chunk text for ids found in the graph.
* [search_chunks](search-chunks.md) - Agent tool: search the source text when the graph lacks an entity or path.

# Orchestration (Module C)

* [Orchestration graph](orchestration-graph.md) - The 3-node LangGraph state machine: router, investigator, compliance.
* [Compliance checks](compliance-checks.md) - Deterministic findings shown to the human reviewer.
* [MCP server](mcp-server.md) - `nexo mcp`: the knowledge graph as seven read-only MCP tools.
* [Evaluation](evaluation.md) - Extraction, retrieval and LLM-as-a-judge metrics.
