---
type: Component
title: Evaluation
description: Extraction, retrieval and LLM-as-a-judge metrics, recorded as OTEL span attributes.
resource: ../../src/nexo/evaluation.py
tags: [evaluation, metrics, llm-as-a-judge]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: evaluation
    resource: ../../src/nexo/evaluation.py
    title: Evaluation module
  - id: aws-health
    resource: ../references/aws-rag-healthcare.md
    title: AWS guidance, RAG for healthcare use cases
---

# Metrics

| Metric | Definition |
|---|---|
| `extraction.precision` | Matching predicted triples / predicted triples. A match is the same head key, relation and tail key, after mapping both sides onto the ontology. |
| `extraction.recall` | Matching triples / gold triples. |
| `extraction.capture_rate` | (matches − false values) / gold, floored at 0. A false value has the right head and predicate but the wrong tail. |
| `retrieval.context_precision` | Fetched chunks that are relevant / fetched chunks. |
| `retrieval.context_recall` | Fetched chunks that are relevant / relevant chunks. |
| `generation.citation_precision` | Cited chunks that are relevant / cited chunks. |
| `generation.answer_contains` | Share of the case's `answer_contains` phrases found in the answer (case-insensitive). Needs no judge model. |
| `generation.faithfulness` | Judge score 1–5: is every claim supported by the cited text? |
| `generation.relevancy` | Judge score 1–5: does the answer address the question? |

`judge_pairwise` compares two answers in both orders and returns a tie when the orders disagree,
which cancels position bias. These metric families follow the healthcare RAG guidance.[^aws-health]

Use a judge model different from the generator (`JUDGE_MODEL`, `JUDGE_BASE_URL`) to limit
self-preference. How to run it: [Evaluation playbook](../operations/evaluation.md). Moving the
runner and generic judges to LangSmith is proposed in
[ADR-0011](../decisions/adr-0011-langsmith-adoption.md); the extraction and retrieval metrics would
stay as custom evaluators.[^evaluation]

[^evaluation]: Evaluation module
[^aws-health]: AWS guidance, RAG for healthcare use cases
