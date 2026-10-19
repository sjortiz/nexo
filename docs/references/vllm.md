---
type: Reference
title: vLLM
description: The open-source inference server for Nexo's GPU deployment - OpenAI-compatible API, guided JSON decoding, tool-call parsing and embeddings.
resource: https://github.com/vllm-project/vllm
tags: [reference, core-stack, inference, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-04-19T00:00:00Z
sources:
  - id: docs
    resource: https://docs.vllm.ai/en/latest/
    title: vLLM documentation
  - id: tools
    resource: https://docs.vllm.ai/en/latest/features/tool_calling.html
    title: vLLM tool calling
  - id: structured
    resource: https://docs.vllm.ai/en/latest/features/structured_outputs.html
    title: vLLM structured outputs
  - id: compose
    resource: ../../docker-compose.yml
    title: Nexo compose stack
---

# Role in Nexo

vLLM serves the models in the GPU deployment (`docker compose --profile gpu`): Llama 3.3 70B for chat
and BAAI/bge-m3 for embeddings, behind an OpenAI-compatible API that LangChain and Strands both speak.[^docs]
On Macs and smaller machines, [Ollama](ollama.md) takes its place with the same API.

# Features used

| Feature | How Nexo uses it |
|---|---|
| OpenAI-compatible `/v1/chat/completions` and `/v1/embeddings` | Every model call; the server is just a base URL per role ([configuration](../operations/configuration.md)). |
| Structured outputs (`response_format` json_schema) | Extraction, routing, resolution and judging are constrained to Pydantic schemas at decode time.[^structured] |
| Tool calling: `--enable-auto-tool-choice --tool-call-parser llama3_json` | Required for the Strands agent; without it Llama answers in prose and never calls tools.[^tools] |
| Continuous batching | Ingestion sends chunks in parallel (`EXTRACTION_CONCURRENCY`) for throughput. |
| Pooling runner for embedding models | A second vLLM instance serves bge-m3. |

The compose file requires an explicit `VLLM_IMAGE_TAG` so the image is always pinned.[^compose]
GPU passthrough is not exercised by this project's tests ([Local on Linux](../operations/local-linux.md)).

[^docs]: vLLM documentation
[^tools]: vLLM tool calling
[^structured]: vLLM structured outputs
[^compose]: Nexo compose stack
