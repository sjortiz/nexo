---
type: Reference
title: Ollama
description: Local model server for Apple Silicon - OpenAI-compatible chat, structured outputs, tool calling and embeddings from one daemon - and the capabilities Nexo relies on.
resource: https://github.com/ollama/ollama
tags: [reference, local, apple-silicon, inference]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: compat
    resource: https://github.com/ollama/ollama/blob/main/docs/api/openai-compatibility.mdx
    title: Ollama OpenAI compatibility
  - id: faq
    resource: https://github.com/ollama/ollama/blob/main/docs/faq.mdx
    title: Ollama FAQ
  - id: context
    resource: https://github.com/ollama/ollama/blob/main/docs/context-length.mdx
    title: Ollama context length
  - id: library
    resource: https://ollama.com/library
    title: Ollama model library
---

# Capabilities Nexo uses (Ollama 0.40)

| Nexo call | Ollama support |
|---|---|
| `response_format: json_schema` (extraction, router, resolution, judge) | Supported; the schema is passed through, `name`/`strict` ignored. GGUF models enforce it with llama.cpp grammars, MLX models with xgrammar.[^compat] |
| Streamed tool calls (Strands agent) | Supported, including `stream_options.include_usage`; `tool_choice` is not supported (Nexo does not send it). |
| `/v1/embeddings` | Supported for string or list input. |
| Several models in memory | `OLLAMA_MAX_LOADED_MODELS` (default 3), `OLLAMA_KEEP_ALIVE` (`-1` = never unload), `OLLAMA_NUM_PARALLEL`.[^faq] |

# Settings that matter

- `OLLAMA_CONTEXT_LENGTH`: the default depends on detected GPU memory (4096 below ~23 GiB) and cannot be
  changed through the OpenAI API; set it explicitly.[^context]
- `OLLAMA_KV_CACHE_TYPE=q8_0`: halves KV-cache memory; global to all models.
- `OLLAMA_HOST`: keep `127.0.0.1`; Ollama has no authentication.

# Models referenced by Nexo's local profiles

From the Ollama library:[^library] `qwen3:30b-a3b-instruct-2507-q4_K_M` (19 GB, tools, no thinking),
`qwen3.6:35b-a3b-q4_K_M` (24 GB, tools, thinking), `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB),
`llama3.1:8b-instruct-q4_K_M` (4.9 GB, tools), `bge-m3` (1.2 GB, embedding), `qwen3-embedding:0.6b` (639 MB,
embedding).

# Alternatives considered

LM Studio (MLX engine, closed source, auto-evicts JIT-loaded models), `llama-server` (router mode
needed for several models), `mlx_lm.server` (no structured output or embeddings) and `vllm-metal` (one
server per model; embeddings experimental). See
[ADR-0012](../decisions/adr-0012-local-apple-silicon.md).

[^compat]: Ollama OpenAI compatibility
[^faq]: Ollama FAQ
[^context]: Ollama context length
[^library]: Ollama model library
