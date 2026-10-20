---
type: Architecture Decision
title: "ADR-0009: OpenTelemetry GenAI conventions, official instrumentation, content off"
description: Use the official GenAI instrumentation for inference spans, Strands' native agent spans, and the GenAI conventions for Nexo's own spans; keep message content out of telemetry by default.
tags: [adr, observability, opentelemetry, privacy]
status: draft
decision_status: accepted
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
stale_after: 2027-01-19T00:00:00Z
sources:
  - id: semconv
    resource: ../references/opentelemetry-genai-conventions.md
    title: OpenTelemetry GenAI semantic conventions
  - id: test
    resource: ../../tests/test_agent.py
    title: Content-capture test
---

# Context

The first version emitted hand-named spans from a LangChain callback (`chat`, `gen_ai.prompt`,
`gen_ai.tool.input`), wrapped the agent in a duplicate span, and had no metrics. The GenAI
conventions and an official Python instrumentation now exist; and Strands, by default, writes message
content into span events.

# Decision

- Inference and embedding spans come from `opentelemetry-instrumentation-genai-openai` (pinned beta),
  which covers every vLLM call through the openai SDK: LangChain and Strands alike.[^semconv]
- Strands owns `invoke_agent` / `execute_tool`; Nexo adds attributes to those spans instead of
  creating its own.
- Nexo emits `invoke_workflow`, node spans, `retrieval` and database CLIENT spans per the conventions.
- Content is off by default (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=no_content`), and
  Strands is put in redaction mode; a test proves no content leaks.[^test]
- **Amended 2026-10-20:** traces identify who asked and which question without the question: `user.id`,
  `session.id` and a keyed hash of the question (`nexo.query.hash`) are always on the request span. The
  question's text is a separate, narrower opt-in than content capture (`telemetry.queryText`), kept only
  for self-hosted backends. Review pauses are no longer recorded as exceptions, which exported the
  question and the draft answer.

# Options considered

| Option | Against |
|---|---|
| Keep the callback handler | Non-standard names; duplicate inference spans once any instrumentation is added. |
| `opentelemetry-instrumentation-openai-v2` | Superseded; unpinned dependency on a util package that removed a module it imports. |
| LangSmith SDK tracing in OTel mode | Puts graph state into spans unless hide flags are set; ties span shape to a vendor SDK. |

# Consequences

- The conventions are in development: attribute and metric names may change; the beta packages are
  pinned and `stale_after` marks this decision for review.
- Two `chat` spans exist per agent model call (Strands wrapper + HTTP client span).

[^semconv]: OpenTelemetry GenAI semantic conventions
[^test]: Content-capture test
