---
type: Reference
title: Strands Agents SDK
description: The open-source agent SDK behind Nexo's relationship investigator - the model-driven tool loop, tools as decorated functions, budgets and native OpenTelemetry.
resource: https://github.com/strands-agents/sdk-python
tags: [reference, core-stack, agents, strands]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: docs
    resource: https://strandsagents.com/docs/
    title: Strands Agents documentation
  - id: repo
    resource: https://github.com/strands-agents/sdk-python
    title: Strands Agents Python SDK
  - id: code
    resource: ../../src/nexo/agents/investigator.py
    title: Nexo investigator
  - id: tools
    resource: ../../src/nexo/agents/tools.py
    title: Nexo investigation tools
---

# Role in Nexo

The investigator is a Strands `Agent`: given a goal, the graph schema and two tools, the model decides
which Cypher to run and which chunks to read until the evidence suffices. Nothing scripts the order or
depth.[^docs] Pinned as `strands-agents[openai]>=1.61,<2`. Details:
[Investigator agent](../components/investigator-agent.md).

# Features used

| Feature | Where | Why |
|---|---|---|
| `Agent(model, tools, system_prompt, name, description, trace_attributes)` | `Investigator._build_agent`[^code] | One fresh agent per investigation; trace attributes carry the conversation id. |
| `@tool` on closures | `build_investigation_tools`[^tools] | Tools bound to a per-run budget, clearance and evidence ledger; docstrings become the tool specs. |
| `OpenAIModel` (OpenAI-compatible provider) | `build_strands_model` | Talks to vLLM or Ollama; tests inject a scripted client to run the real loop offline. |
| `invoke_async(..., limits=Limits(turns, total_tokens))` | `Investigator.ainvestigate` | Hard budget on loop turns and tokens; `stop_reason` reports which tripped. |
| Native OpenTelemetry (`invoke_agent`, `execute_tool`, `chat` spans) | global tracer provider | Agent internals traced with the GenAI conventions; content redaction via `OTEL_SEMCONV_STABILITY_OPT_IN`.[^repo] |

See [ADR-0001](../decisions/adr-0001-strands-agent-inside-langgraph-task.md) and the
[LangGraph to Strands handoff](../architecture/langgraph-strands-handoff.md).

[^docs]: Strands Agents documentation
[^repo]: Strands Agents Python SDK
[^code]: Nexo investigator
[^tools]: Nexo investigation tools
