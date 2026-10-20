# %% [markdown]
# # Nexo, step by step
#
# This walkthrough builds a knowledge graph from two short documents and answers a question that
# neither document answers alone. Each step calls the same code Nexo runs in production, one piece
# at a time, so you can see what every stage adds.
#
# The documents are fictional. One is an investor note saying **Acme Holdings** (also written
# "AHI") bought **Beta Labs**. The other is a clinic's supplier review saying **Gamma Clinics** buys
# test kits from Beta Labs. The question: *how is Acme Holdings connected to Gamma Clinics?*
#
# **Run it yourself:** follow steps 1 to 6 of [Run it](index.html#quickstart) (models, Neo4j and
# `.env`). Then open [`examples/walkthrough.ipynb`](https://github.com/sjortiz/nexo/blob/main/examples/walkthrough.ipynb)
# in Jupyter (`pip install -e ".[notebook]"`, then `jupyter lab`), or run
# `python examples/walkthrough.py`. The outputs below come from a real run of that notebook on an
# Apple M5 Pro with the `m5-pro-24` profile. Models are not deterministic, so your wording will differ.

# %% [markdown]
# ## Setup
#
# `Settings` reads the repository's `.env`, so the hardware profile picks the models. Three overrides
# keep this run separate from your own data: its own chunk store file, an in-memory checkpointer, and
# the example relation vocabulary. Message capture in traces is **off by default**; it is turned on
# here so a later step can show the exact queries the agent wrote.

# %%
import json
import logging
import sys
from datetime import date
from pathlib import Path

from nexo.config import Settings

log = logging.getLogger("walkthrough")
if not log.handlers:  # re-running this cell must not duplicate output
    log.addHandler(logging.StreamHandler(sys.stdout))
log.setLevel(logging.INFO)
log.propagate = False
logging.getLogger().setLevel(logging.ERROR)  # keep library logs out of the way


def show(value):
    """Log a value as readable text: models and dicts as JSON."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    log.info(value if isinstance(value, str) else json.dumps(value, indent=2, default=str, ensure_ascii=False))


# The repository root, whether this runs as a script or as a notebook in examples/.
ROOT = next(p for p in (Path.cwd(), *Path.cwd().parents) if (p / "pyproject.toml").is_file())
settings = Settings(
    _env_file=ROOT / ".env",
    vector_store_path=ROOT / ".data/walkthrough/chunks.json",
    checkpoint_backend="memory",
    ontology_path=ROOT / "deploy/ontology.example.json",
    otel_instrumentation_genai_capture_message_content="span_only",
)

# %% [markdown]
# Before anything else, confirm every model role has a server that answers and holds its model. This
# is what `nexo models --check` runs.

# %%
from nexo.llm import check_model_servers, resolved_models

show([{key: row.get(key) for key in ("role", "model", "check")}
      for row in check_model_servers(settings, resolved_models(settings))])

# %% [markdown]
# Collect traces in memory, so later steps can look at what happened inside each call. In a real
# deployment these spans go to an OpenTelemetry collector instead.

# %%
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nexo.telemetry import setup_telemetry

spans = InMemorySpanExporter()
setup_telemetry(settings).add_span_processor(SimpleSpanProcessor(spans))

# %% [markdown]
# ## 1. Split documents along their structure
#
# Retrieval works on chunks, and a chunk is only useful if it holds one coherent idea. Nexo splits on
# Markdown headings first and only then by size, and keeps the heading path with each chunk. Chunk ids
# are derived from the content, so re-ingesting an unchanged document is a no-op.

# %%
from nexo.storage.chunking import split_document
from nexo.storage.schemas import DocumentMetadata

DOCS = {
    "acme-portfolio-note": DocumentMetadata(source_id="acme-portfolio-note", title="Acme Holdings: 2025 portfolio note",
                                            doc_type="memo", effective_date=date(2025, 6, 30)),
    "gamma-supplier-review": DocumentMetadata(source_id="gamma-supplier-review",
                                              title="Gamma Clinics: annual supplier review",
                                              doc_type="review", effective_date=date(2025, 9, 15)),
}
texts = {source_id: (ROOT / "examples/corpus" / f"{source_id}.md").read_text() for source_id in DOCS}

chunks = split_document(texts["acme-portfolio-note"], DOCS["acme-portfolio-note"],
                        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)
show([{"chunk_id": c.chunk_id, "section": " > ".join(c.section_path), "text": c.text[:60] + "..."} for c in chunks])

# %% [markdown]
# ## 2. Classify each chunk's sensitivity
#
# Every chunk gets a level: public, internal, PII or PHI. Callers only see chunks at or below their
# clearance, and later every node and edge in the graph inherits a level from the chunks that support
# it. Here a regular-expression detector finds the email address in the Leadership section.

# %%
from nexo.storage.sensitive_data import RegexDetector, classify

detector = RegexDetector()
levels = []
for chunk in chunks:
    found = detector.detect(chunk.text)
    levels.append({"section": chunk.section_path[-1], "detected": [d.category for d in found],
                   "sensitivity": classify(found, chunk.sensitivity).value})
show(levels)

# %% [markdown]
# ## 3. Extract facts with a schema
#
# The extraction model reads one chunk at a time and must answer in a JSON schema: a list of
# (head entity, relation, tail entity) facts, each with a **verbatim quote** as evidence. The server
# constrains the output to the schema. Nexo then drops any fact whose quote does not appear in the
# chunk, which is the first guard against invented facts.

# %%
from nexo.llm import build_chat_model
from nexo.storage.ingest import KnowledgeGraphIngestor
from nexo.storage.ontology import Ontology

ontology = Ontology.load(settings.ontology_path)
extractor = KnowledgeGraphIngestor(llm=build_chat_model(settings, role="extraction"), chunk_store=None,
                                   graph_writer=None, detector=detector, settings=settings, ontology=ontology)
triples, report = extractor.extract_triples(texts["acme-portfolio-note"], DOCS["acme-portfolio-note"])

for t in triples:
    show(f"({t.triple.head.name}) -[{t.relation}]-> ({t.triple.tail.name})\n    evidence: \"{t.triple.evidence}\"")
show({"extracted": report.triples_extracted, "kept": len(triples), "dropped": report.dropped,
      "relations": report.relations})

# %% [markdown]
# ## 4. Map relations onto a vocabulary
#
# Models phrase the same fact many ways: "bought", "acquired", "was acquired by". A relation vocabulary
# maps synonyms onto one canonical relation, flips inverses so direction is consistent, and checks the
# entity types. Anything it does not know is kept as `RELATED_TO` with the original predicate, rather
# than lost.

# %%
from nexo.storage.schemas import EntityType

ORG = EntityType.ORGANIZATION
for predicate in ["BOUGHT", "ACQUIRED_BY", "BUYS_FROM", "SPONSORS"]:
    mapping = ontology.map(predicate, ORG, ORG)
    show(f"{predicate:<12} -> {mapping.relation:<10} reversed: {mapping.swap}")

# %% [markdown]
# ## 5. Resolve names to one entity
#
# "Acme Holdings Inc.", "AHI" and, in the other document, "Acme" are one company. Resolution happens in
# layers: a normalized key removes legal suffixes and articles; aliases stated in the text merge into
# the most complete name; and when a document is written to the graph, a model compares new names with
# existing entities of the **same type only**. Every merge is recorded so it can be undone.

# %%
from nexo.storage.resolution import EntityResolver
from nexo.storage.schemas import EntityRef, normalize_name

show({name: normalize_name(name, ORG) for name in ["Acme Holdings Inc.", "The Acme Holdings, Inc.", "AHI"]})

mentions = [EntityRef(name="Acme Holdings Inc.", type=ORG, aliases=["AHI"]),
            EntityRef(name="AHI", type=ORG), EntityRef(name="Beta Labs", type=ORG)]
resolved = EntityResolver(catalog=None, llm=None, mode="deterministic").resolve(mentions)
show({key: f"{r.method.value} -> {r.key}" for key, r in resolved.items()})

# %% [markdown]
# ## 6. Write both documents to the graph
#
# `ingest` runs every step above and writes the result: chunks and their vectors to the chunk store,
# and documents, chunks, entities and relations to Neo4j. Re-ingesting a document replaces what it
# contributed before. The report counts what was written, how relations were mapped and how names were
# resolved.

# %%
from nexo.service import open_ingestor

with open_ingestor(settings) as ingestor:
    for source_id, metadata in DOCS.items():
        report = ingestor.ingest(texts[source_id], metadata)
        show(report.model_dump(include={"source_id", "chunk_count", "triples_written", "sensitivity",
                                        "relations", "resolution", "dropped"}))

# %% [markdown]
# ## 7. Look at the graph
#
# Every relation keeps the chunk it came from, so any answer can be traced back to its source. Note
# where "Acme" from the clinic's review ended up, and that each entity carries the sensitivity of the
# least sensitive chunk that supports it: Jane Roe appears only in the PII chunk.
#
# Look closely and you may also find mistakes. A 9-billion-parameter model sometimes reverses a
# relation or picks one its quote does not support. Nexo does not hide that: every edge points at its
# chunk, the agent reads that text before answering, and a person reviews the answer.

# %%
from nexo.storage.graph_store import GraphStore

graph = GraphStore.from_settings(settings, read_only=True)
show(graph.run_read_query("""
    MATCH (a:Entity)-[r]->(b:Entity)
    RETURN a.name AS head, type(r) AS relation, b.name AS tail, r.chunk_ids AS chunk_ids
    ORDER BY head, relation, tail""").rows)
show(graph.run_read_query("""
    MATCH (e:Entity) WHERE e:Organization OR e:Person
    RETURN e.name AS name, e.type AS type, e.aliases AS aliases, e.source_ids AS documents,
           ['public', 'internal', 'pii', 'phi'][e.sensitivity_rank] AS sensitivity
    ORDER BY type, name""").rows)

# %% [markdown]
# ## 8. Search: keywords, meaning, or both
#
# The agent can start from a search over chunks. Keyword search (BM25) finds exact names and codes;
# vector search finds paraphrases; hybrid search combines both rankings with Reciprocal Rank Fusion.
# The query below paraphrases "buys its respiratory test kits from Beta Labs"; compare the three
# rankings.

# %%
from nexo.llm import build_embeddings
from nexo.storage.vector_store import ChunkFilter, LocalChunkStore

query = "Which company provides diagnostics to the clinic?"
for mode in ("keyword", "vector", "hybrid"):
    store = LocalChunkStore(build_embeddings(settings), settings.vector_store_path, mode=mode)
    hits = store.search(query, k=3, chunk_filter=ChunkFilter())
    show(f"{mode:<8} " + " | ".join(f"{h.source_id} > {h.section_path[-1]}" for h in hits))

# %% [markdown]
# ## 9. Let a model query the graph, safely
#
# The agent writes its own Cypher, so Nexo never trusts it: queries run in a read-only transaction with
# a timeout and a row cap, under a read-only database user when configured. Before that, a validator
# rejects anything that is not a single read query.

# %%
from nexo.storage.graph_store import UnsafeCypherError, validate_read_only_cypher

for cypher in ["MATCH (o:Organization) RETURN o.name LIMIT 5",
               "MATCH (o:Organization) DETACH DELETE o",
               "CALL dbms.components()"]:
    try:
        validate_read_only_cypher(cypher)
        show(f"allowed   {cypher}")
    except UnsafeCypherError as exc:
        show(f"rejected  {cypher}\n          {exc}")

# %% [markdown]
# ## 10. Investigate with an agent
#
# The investigator is a Strands agent with two tools: `query_knowledge_graph` (read-only Cypher) and
# `fetch_vector_chunks` (the exact text behind a chunk id). It decides what to follow. It may only cite
# chunks it actually fetched, and it stops at a turn and token budget.

# %%
from nexo.agents.investigator import Investigator
from nexo.llm import build_strands_model

chunk_store = LocalChunkStore(build_embeddings(settings), settings.vector_store_path)
investigator = Investigator(model=build_strands_model(settings), graph=graph, chunks=chunk_store,
                            settings=settings, schema_text=graph.describe_schema())
spans.clear()
output = investigator.investigate("How is Acme Holdings connected to Gamma Clinics?")
show(output.answer)
show({"citations": output.citations, "tool_calls": [f"{c.tool}: {c.detail}" for c in output.tool_calls],
      "stop_reason": output.stop_reason})

# %% [markdown]
# The trace shows what the agent did. These are the Cypher queries it wrote, in order:

# %%
for span in spans.get_finished_spans():
    if span.name == "execute_tool query_knowledge_graph":
        show(json.loads(span.attributes["gen_ai.tool.call.arguments"])["cypher_query"].strip())

# %% [markdown]
# ## 11. Put it in a workflow with human review
#
# The agent is one step of a fixed LangGraph workflow. A **router** checks the request first. The
# **investigator** runs inside a retried task. **Compliance** checks the draft (uncited claims,
# citations to text the agent never read, sensitive data) and then pauses the run until a person
# decides. The pause is checkpointed, so the decision can come from another process, days later.

# %%
from nexo.orchestration.graph import workflow_mermaid

show(workflow_mermaid())

# %% [markdown]
# Start a run. It stops at the review step and returns the packet a reviewer sees.

# %%
from contextlib import ExitStack

from nexo.service import open_runtime

stack = ExitStack()
runtime = stack.enter_context(open_runtime(settings))
run = runtime.start("How is Acme Holdings connected to Gamma Clinics?")
show({"status": run.status, "routed_to": run.routing_target})
show({key: run.pending_review[key] for key in ("draft_answer", "citations", "compliance_report")})

# %% [markdown]
# A reviewer approves it, and only then is the answer released.

# %%
from nexo.orchestration.state import ReviewDecision

done = runtime.review(run.thread_id, ReviewDecision(approval_status="approved", reviewer="reviewer@example.com"))
show({"status": done.status, "approval": done.approval_status})
show(done.final_response)

# %% [markdown]
# Requests outside the knowledge base end at the router, before any agent work:

# %%
off_topic = runtime.start("Write a short poem about the sea.")
show({"status": off_topic.status, "routed_to": off_topic.routing_target, "reason": off_topic.routing_reason})

# %% [markdown]
# ## 12. Clearance decides what the agent can see
#
# The Leadership chunk holds an email address, so it is PII. A caller with the default `internal`
# clearance cannot see that chunk, and the agent cannot use it: it should report what it could not
# find rather than guess. With `pii` clearance, the same question gets a different answer.
#
# If the `internal` answer still names the person, that name came from a graph query that returned a
# plain value (`p.name`) rather than a whole node. Nexo redacts nodes above the caller's clearance,
# but on Neo4j Community it cannot tell where a plain value came from. Neo4j Enterprise closes this
# gap with role-based access on the `:PII` and `:PHI` labels; on Community, withheld chunks and human
# review are the remaining controls
# ([ADR-0006](https://github.com/sjortiz/nexo/blob/main/docs/decisions/adr-0006-sensitivity-and-caller-clearance.md)).

# %%
question = "Who is the chief executive of Beta Labs?"
for clearance in ("internal", "pii"):
    result = runtime.start(question, clearance=clearance)
    draft = (result.pending_review or {}).get("draft_answer")
    show(f"[{clearance}] " + (draft or f"no answer released (approval: {result.approval_status})"))

# %% [markdown]
# ## 13. Use the graph from other clients over MCP
#
# `nexo mcp` exposes the same graph to Claude Code or any MCP client as read-only tools, with the same
# clearance and Cypher safeguards. Here the server runs in-process.

# %%
from mcp.client import Client

from nexo.aio import run_sync
from nexo.mcp_server import build_mcp_server

server = build_mcp_server(graph=graph, chunks=chunk_store, settings=settings)


async def find_entity(name):
    async with Client(server) as client:
        tools = await client.list_tools()
        result = await client.call_tool("nexo_find_entities", {"name": name})
        return [t.name for t in tools.tools], result.structured_content


tool_names, found = run_sync(find_entity("Acme"))  # also works inside a notebook's event loop
show(tool_names)
show(found)

# %% [markdown]
# ## 14. Read the trace
#
# Every run produces one trace: the workflow, each node, the agent, each model call, each tool call and
# each database query, named with the OpenTelemetry GenAI conventions. This is the trace of the run
# that paused for review in step 11.

# %%
finished = spans.get_finished_spans()
children = {}
for s in finished:
    children.setdefault(s.parent.span_id if s.parent else None, []).append(s)
root = next(s for s in finished if s.name.startswith("invoke_workflow")
            and s.attributes.get("gen_ai.conversation.id") == run.thread_id
            and s.attributes.get("nexo.operation") == "start")


def outline(span, depth=0):
    lines = [f"{'  ' * depth}{span.name}"]
    for child in sorted(children.get(span.context.span_id, []), key=lambda s: s.start_time):
        lines += outline(child, depth + 1)
    return lines


show("\n".join(outline(root)))

# %% [markdown]
# ## Clean up
#
# Close the runtime and the graph connection. The two documents stay in the graph; ingesting them again
# replaces them.

# %%
stack.close()
graph.close()

# %% [markdown]
# ## Where to go next
#
# - [How local setup works](https://github.com/sjortiz/nexo/blob/main/docs/operations/local-setup.md):
#   profiles, roles and ports.
# - [Architecture docs](https://github.com/sjortiz/nexo/blob/main/docs/index.md): the data model, the
#   handoff between LangGraph and the agent, and the design decisions behind each step.
# - Ingest your own documents with `nexo ingest`, and ask with `nexo ask`.
