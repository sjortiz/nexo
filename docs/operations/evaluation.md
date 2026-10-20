---
type: Playbook
title: Evaluation playbook
description: Run the investigator over a golden set and read the retrieval and judge metrics.
resource: ../../src/nexo/cli.py
tags: [operations, evaluation]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-19T20:14:37Z }
sources:
  - id: cli
    resource: ../../src/nexo/cli.py
    title: nexo eval
---

# Golden set

One JSON object per line:

```json
{"question": "How is Acme Holdings connected to Gamma Clinics?", "relevant_chunk_ids": ["chk_3f1a..."], "reference_answer": "Acme owns Beta Labs, which supplies Gamma Clinics."}
```

Optional fields: `answer_contains` (phrases a correct answer states; scored without a judge),
`strategy` (`graph`, `vector` or `hybrid`, default `hybrid`) and `clearance` (`public`, `internal`, `pii`,
`phi`; default `DEFAULT_CLEARANCE`). `nexo eval` skips the router, so `strategy` and `clearance` replay what
the router chose and who is asking. Each result also logs the answer, the tool call count and the stop reason.

[`examples/golden/acme-gamma.jsonl`](../../examples/golden/acme-gamma.jsonl) covers the two sample documents
in `examples/corpus`: a multi-hop chain through a missing entity (Delta Devices), a direct lookup, a
question that needs both documents and a question the documents do not answer. Ingest both documents
first. The first three cases need `pii` clearance because the leadership chunk (it has an email address)
is classified `pii`.

Build it from real reviewer-approved threads: the approved answer becomes `reference_answer`, and the
chunks the reviewer confirmed become `relevant_chunk_ids`.

# Steps

1. Ingest the corpus the golden set refers to (chunk ids are deterministic, so they stay stable as
   long as the text does).
2. Optionally set `JUDGE_MODEL` (and `JUDGE_BASE_URL`) to a model different from the generator.
3. Run `LOG_FORMAT=json nexo eval golden.jsonl > results.jsonl`.
4. Each case is logged with the question plus the [metrics](../components/evaluation.md) in its
   `result` field (`LOG_FORMAT=json` for one object per line), and is also an `eval.case` span with
   `eval.*` attributes.

`nexo eval` calls the investigator directly. It skips the router and human review, so it measures
retrieval and generation only.[^cli]

# Extraction against gold triples

`nexo eval-extraction gold.jsonl` runs chunking and extraction only (no graph or vector writes) on
each case and scores the result:

```json
{"text": "Acme Holdings acquired Beta Labs in 2024.", "triples": [{"head": {"name": "Acme Holdings", "type": "Organization"}, "relation": "ACQUIRED", "tail": {"name": "Beta Labs", "type": "Organization"}, "evidence": "Acme Holdings acquired Beta Labs"}]}
```

Gold relations go through the same ontology mapping, so a gold `ACQUIRED_BY` matches a predicted
`ACQUIRED` in the right direction. Each case and the overall precision, recall and capture rate are
logged as `result` records. Use it before and after changing the extraction prompt or the ontology.

# Reading results

| Symptom | Likely cause |
|---|---|
| Low `context_recall` | Entity names do not match graph keys; extraction missed relations; `AGENT_MAX_TOOL_CALLS` too low. |
| Low `context_precision` | Unbounded path queries; too many seeds (`SEED_CHUNKS`). |
| Low `citation_precision`, high recall | The agent cites everything it read; tighten the prompt's citation rule. |
| Answers say an entity "does not exist" that the text mentions | The agent never searched the text; check for `search_chunks` calls in the trace. |
| Low `faithfulness` | Claims beyond the evidence; check `ungrounded_citations` rates in compliance. |

[^cli]: nexo eval
