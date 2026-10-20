---
type: Playbook
title: Testing
description: Offline suite, Neo4j integration tests, OKF conformance and mutation checks.
resource: ../../tests/
tags: [operations, testing, quality]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: tests
    resource: ../../tests/
    title: Test suite
---

# Suites

| Suite | Needs | Covers |
|---|---|---|
| `tests/test_storage.py` | nothing | Schemas, normalization, chunking, Cypher guard, chunk filter, chunk store, write planning. |
| `tests/test_ingest.py` | nothing | Validation retry, evidence grounding, previous-chunk context, ontology mapping, resolution, PII classification, failed chunks, re-ingestion, redaction. |
| `tests/test_resolution.py` | nothing | Ontology mapping (synonyms, inverses, type constraints, fallback), name normalization, resolution including the cases that must not merge. |
| `tests/test_logging.py` | nothing | Trace-correlated log records, JSON and text formats, handler setup. |
| `tests/test_agent.py` | nothing | Both tools, the real Strands event loop against a scripted OpenAI-compatible client, Strands span attributes, and that message content reaches traces only when capture is on. |
| `tests/test_graph.py` | nothing | Routing, interrupt/resume, review validation, blocking findings, failures, retries, clearance, SQLite durability, GenAI span shape, and the official instrumentation's single `chat {model}` span per vLLM call. |
| `tests/test_retrieval.py` | nothing | BM25 (IDF, accent folding), keyword/vector/hybrid modes, RRF fusion, filtering, index invalidation and persistence. |
| `tests/test_local_profiles.py` | nothing | Profile parsing and tiers (each fits its memory), settings resolution and precedence, the setup script resolving the same models, the playbook listing every model. |
| `tests/test_models_command.py` | nothing | `nexo models`: per-role resolution across profiles and explicit settings, no secrets in output, `--check` against a fake OpenAI `/models` server (served, missing, other tag, unreachable) and its exit code. |
| `tests/test_site.py` | nothing | Every GitHub Pages page: self-contained, anchors, cross-page and repository links resolve; quickstart commands match the CLI, compose and env template. |
| `tests/test_walkthrough.py` | nothing | The walkthrough page equals the executed notebook, which equals the script's cells run once in order without errors; Nexo imports exist; highlighting preserves code exactly. |
| `tests/test_aio.py` | nothing | `run_sync` works with and without a running event loop (notebooks) and keeps the caller's context. |
| `tests/test_model_roles.py` | nothing | Per-role endpoints and fallbacks, reasoning effort only for JSON roles, env templates use only real settings. |
| `tests/test_live_models.py` | model servers, `NEXO_LIVE=1` | Real models: schema-valid extraction and routing, embeddings ranking (English and Spanish), agent tool calls, GenAI spans. |
| `tests/test_mcp_server.py` | nothing | Every MCP tool over the real protocol (in-process and `nexo mcp` over stdio), clearance, parameterized lookups, rejected writes, JSON-only stdout. |
| `tests/test_claude_skill.py` | nothing | The skill's frontmatter, tool list and phases, and `.mcp.json`, in sync with the MCP server. |
| `tests/test_docs_okf.py` | nothing | This bundle conforms to OKF v0.2; links, sources and footnotes resolve; every setting is documented. |
| `tests/test_neo4j_integration.py` | Neo4j | Real Cypher: merging, labels, lexical graph order, alias withdrawal, RELATED_TO per predicate, cross-document resolution through the real candidate query, server-side write refusal, row cap, database span, end-to-end ingest → investigate → approve. |

The suite runs on macOS and Linux (verified in `python:3.12-slim` on Linux aarch64 with Neo4j 5.26).
vLLM is never required: the agent tests drive the real Strands loop with scripted chat completions, so
tool wiring, message formatting and stop reasons are exercised without a GPU.[^tests]

# Steps

```bash
pip install -e ".[dev]"
ruff check src tests
pytest                                   # integration tests skip without NEO4J_TEST_URI

podman run -d --name nexo-neo4j-test -p 127.0.0.1:7688:7687 \
  -e NEO4J_AUTH=neo4j/nexo-test-pass neo4j:5.26-community
NEO4J_TEST_URI=bolt://127.0.0.1:7688 NEO4J_TEST_PASSWORD=nexo-test-pass pytest
podman rm -f nexo-neo4j-test
```

# Writing tests

Cover the failure path, not just the happy path. For each safeguard, check that the test fails when the
safeguard is removed (a mutation check). The last check broke 36 safeguards one at a time and the
suite failed for every one, including:

- evidence grounding, graph redaction, chunk clearance, the Cypher guard, the evidence ledger
- ungrounded citations, the acknowledgement requirement, the clearance clamp, review validation
- transient retries, router fail-closed, stale-delete keep set, Luhn filter
- previous-chunk context, inverse swaps, ontology type constraints, the RELATED_TO fallback,
  name normalization
- same-type resolution, offered-candidates-only, ambiguous aliases, in-document merges,
  deterministic mode, self-loops after resolution
- chunk ordering, alias withdrawal, RELATED_TO per predicate, `db.query.text` opt-in
- Strands content redaction, span nesting, interrupt marking, the GenAI instrumentation,
  conversation ids, log trace correlation and result payloads

Tests use fakes only at the edges (the vLLM HTTP client, the graph reader); the real Strands loop,
LangGraph runtime, openai SDK and Neo4j are exercised.

[^tests]: Test suite
