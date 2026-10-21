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
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Local model profiles
  - id: experiments
    resource: ../../src/nexo/experiments.py
    title: Langfuse experiments
---

# Golden set

One JSON object per line:

```json
{"question": "How is Acme Holdings connected to Gamma Clinics?", "relevant_chunk_ids": ["chk_3f1a..."], "reference_answer": "Acme owns Beta Labs, which supplies Gamma Clinics."}
```

Optional fields: `answer_contains` (phrases a correct answer states; scored without a judge),
`strategy` (`graph`, `vector` or `hybrid`, default `hybrid`) and `clearance` (`public`, `internal`, `pii`,
`phi`; default `DEFAULT_CLEARANCE`). `nexo eval` skips the router, so `strategy` and `clearance` replay what
the router chose and who is asking. Each result also holds the answer, the tool call count and the stop reason.

[`examples/golden/acme-gamma.jsonl`](../../examples/golden/acme-gamma.jsonl) covers the two sample documents
in `examples/corpus`: a multi-hop chain through a missing entity (Delta Devices), a direct lookup, a
question that needs both documents and a question the documents do not answer. Ingest both documents
first. The leadership chunk has an email address, so it is classified `pii`. The Delta case runs at the default `internal`
clearance and is answered from the masked chunk ([read-time redaction](../components/sensitive-data-detection.md#read-time-redaction));
the two cases after it use `pii` to read the chunk whole. The judge is shown cited chunks as the case's clearance sees them (masked, or dropped if withheld), so a hosted judge never receives the masked-out text.

Build it from real reviewer-approved threads: the approved answer becomes `reference_answer`, and the
chunks the reviewer confirmed become `relevant_chunk_ids`.

# Steps

1. Ingest the corpus the golden set refers to (chunk ids are deterministic, so they stay stable as
   long as the text does).
2. Optionally turn on the judge ([below](#judge-scores)) for faithfulness and relevancy.
3. Run `nexo eval golden.jsonl > results.jsonl`.
4. Each case is one JSON object per line on stdout (logs go to stderr): the question plus the
   [metrics](../components/evaluation.md). It is also an `eval.case` span with `eval.*` attributes.
   A case whose investigation raises (a timeout, the token cap, a store error) is written as
   `{"question": ..., "error": "<exception class>"}` with no scores, the run goes on, and the
   command exits 1 at the end; `eval-extraction` does the same per case.

`nexo eval` calls the investigator directly. It skips the router and human review, so it measures
retrieval and generation only.[^cli]

# In Langfuse

With the langfuse add-on on (`make up LANGFUSE=1` or `addons.langfuse` in `nexo.yaml`), `nexo setup`
sets `LANGFUSE_HOST`, and every `nexo eval` run is also a Langfuse experiment:[^experiments]

| In Langfuse | From |
|---|---|
| Dataset | The golden set, named after the file (`--dataset-name` to change it). One item per case: input `question`, `strategy`, `clearance`; expected output `answer` (`reference_answer`), `relevant_chunk_ids`, `answer_contains`. A rerun updates the same items. |
| Experiment | One per run, named after the agent model and the time (`--run-name` to change it), with the agent and judge models as metadata. |
| Experiment item | Each case's trace, rooted at its `eval.case` span and linked to its dataset item. |
| Scores | Each case's non-null metrics, as numeric scores on that span. |

Compare runs in **Datasets** → the dataset → its experiments. If Langfuse cannot be reached when the
run starts, `nexo eval` stops before running a case; a score that fails later is logged and the run
goes on.

Answers stay off spans, as everywhere else, so Langfuse shows each case's input, expected output,
scores and trace (timings, tool calls, tokens) but not the answer: that is in `results.jsonl`.
Langfuse's own LLM-as-judge evaluators read trace output, so use Nexo's judge instead
([below](#judge-scores)), whose scores arrive as `generation.faithfulness` and `generation.relevancy`.

# Judge scores

Faithfulness and relevancy need the judge role. Profiles of 24 GB and less leave it off to save
memory (`make check` shows `model judge: off`). Use a judge model different from the agent's, so it
does not grade its own answers. `make up` pulls only the profile's models, so pull the judge first:

```sh
ollama pull llama3.1:8b-instruct-q4_K_M
```

For one run, set it on the command:

```sh
JUDGE_MODEL=llama3.1:8b-instruct-q4_K_M nexo eval golden.jsonl
```

To keep it on, name it in `nexo.yaml`; it runs on the profile's Ollama unless `base_url` says otherwise:

```yaml
spec:
  models:
    roles: { judge: { model: llama3.1:8b-instruct-q4_K_M } }
```

The judge adds about 4.9 GB. `make up` lets Ollama keep only the profile's models loaded (two here),
so with a judge it swaps models during a run: prefer the one-run form on a small Mac.[^profiles]

With more memory, a second machine or a GPU server, use a stronger judge: any model on an
OpenAI-compatible endpoint works (`JUDGE_API_KEY` if it needs a key).

```yaml
    roles: { judge: { model: qwen3:30b-a3b-instruct-2507-q4_K_M, base_url: "http://localhost:11435/v1" } }
```

# Extraction against gold triples

`nexo eval-extraction gold.jsonl` runs chunking and extraction only (no graph or vector writes) on
each case and scores the result:

```json
{"text": "Acme Holdings acquired Beta Labs in 2024.", "triples": [{"head": {"name": "Acme Holdings", "type": "Organization"}, "relation": "ACQUIRED", "tail": {"name": "Beta Labs", "type": "Organization"}, "evidence": "Acme Holdings acquired Beta Labs"}]}
```

Gold relations go through the same ontology mapping, so a gold `ACQUIRED_BY` matches a predicted
`ACQUIRED` in the right direction. Each case and the overall precision, recall and capture rate are
written to stdout, one JSON object per line (`"case": "overall"` last). Use it before and after changing the extraction prompt or the ontology.

# Reading results

| Symptom | Likely cause |
|---|---|
| Low `context_recall` | Entity names do not match graph keys; extraction missed relations; `AGENT_MAX_TOOL_CALLS` too low. |
| Low `context_precision` | Unbounded path queries; too many seeds (`SEED_CHUNKS`). |
| Low `citation_precision`, high recall | The agent cites everything it read; tighten the prompt's citation rule. |
| Answers say an entity "does not exist" that the text mentions | The agent never searched the text; check for `search_chunks` calls in the trace. |
| Low `faithfulness` | Claims beyond the evidence; check `ungrounded_citations` rates in compliance. |

[^cli]: nexo eval
[^profiles]: Local model profiles
[^experiments]: Langfuse experiments
