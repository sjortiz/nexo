---
type: Reference
title: LangChain
description: The LangChain packages Nexo uses for model clients, embeddings, text splitting and the local vector index.
resource: https://github.com/langchain-ai/langchain
tags: [reference, core-stack, langchain]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: docs
    resource: https://docs.langchain.com/oss/python/langchain/overview
    title: LangChain documentation
  - id: llm
    resource: ../../src/nexo/llm.py
    title: Nexo model clients
  - id: storage
    resource: ../../src/nexo/storage/vector_store.py
    title: Nexo chunk store
---

# Role in Nexo

LangChain supplies the building blocks around the models: chat and embedding clients for any
OpenAI-compatible server, Markdown-aware splitters, and the in-process vector index.[^docs] Nexo does not
use LangChain agents or chains; orchestration is [LangGraph](langgraph.md) and the agent is
[Strands](strands-agents.md).

# Packages and features used

| Package (pin) | Feature | Where |
|---|---|---|
| `langchain-openai>=1.7,<2` | `ChatOpenAI` for extraction, routing, resolution and judging, with `response_format` json_schema bound for guided decoding | `build_chat_model`, `invoke_structured`[^llm] |
| `langchain-openai` | `OpenAIEmbeddings(check_embedding_ctx_length=False)` so non-OpenAI servers get raw strings | `build_embeddings` |
| `langchain-text-splitters>=1.1,<2` | `MarkdownHeaderTextSplitter` + `RecursiveCharacterTextSplitter` | [Chunking](../components/chunking.md) |
| `langchain-core>=1.6,<2` | `InMemoryVectorStore` (with Nexo's BM25 and RRF on top), `DeterministicFakeEmbedding`, message types, fake chat models in tests | `LocalChunkStore`[^storage] |

Calls through `ChatOpenAI` and `OpenAIEmbeddings` go through the openai SDK, which the OpenTelemetry
GenAI instrumentation traces ([Observability](../operations/observability.md)).

[^docs]: LangChain documentation
[^llm]: Nexo model clients
[^storage]: Nexo chunk store
