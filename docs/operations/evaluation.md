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
`strategy` (`graph`, `vector` or `hybrid`, default `hybrid`), `clearance` (`public`, `internal`, `pii`,
`phi`; default `DEFAULT_CLEARANCE`) and `canned_answer` (score this answer instead of running the agent:
a judge-sanity case, whose `sanity` field says why it must score low). `nexo eval` skips the router, so `strategy` and `clearance` replay what
the router chose and who is asking. Each result also holds the answer, the tool call count and the stop reason.

[`examples/golden/acme-gamma.jsonl`](../../examples/golden/acme-gamma.jsonl) has 26 cases over the four
sample documents in `examples/corpus`: direct lookups, multi-hop chains (Acme to Delta Devices through
Beta Labs and Jane Roe; Arun Mehta to Gamma Clinics through Delta Devices), questions that need two
documents, three the documents do not answer, clearance pairs (the same question at `internal` and at
`pii` or `phi`: the leadership chunk holds an email address and the trial participant's chunk a medical
record number) and two judge-sanity cases with a canned answer that is wrong or beside the point. Ingest
all four documents first. The judge is shown cited chunks as the case's clearance sees them (masked, or
dropped if withheld), so a hosted judge never receives the masked values.

Build it from real reviewer-approved threads: the approved answer becomes `reference_answer`, and the
chunks the reviewer confirmed become `relevant_chunk_ids`.

# Steps

1. Ingest the corpus the golden set refers to (chunk ids are deterministic, so they stay stable as
   long as the text does).
2. Optionally turn on the judge ([below](#judge-scores)) for faithfulness and relevancy.
3. Run `nexo eval golden.jsonl > results.jsonl`. Add `--repeat 3` to run every case three times:
   each row then carries `rep`, and one summary row per question closes the file with the `mean` and
   the `spread` (max minus min) of every metric over the repeats.
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
`ACQUIRED` in the right direction. [`examples/golden/extraction-gold.jsonl`](../../examples/golden/extraction-gold.jsonl)
holds one case per section of the sample corpus, past roles included; a passage that states a directional
relation both ways (A ACQUIRED B and B ACQUIRED A) is an extraction error and both triples are dropped,
counted under `contradictory_direction`. Each case and the overall precision, recall and capture rate are
written to stdout, one JSON object per line (`"case": "overall"` last). Use it before and after changing the extraction prompt or the ontology.

# Reading results

**Noise before differences.** A local model answers the same question differently from run to run, and
one run of the four-case set moved context and citation precision by up to 0.6 on identical code. Before
comparing two versions, run each with `--repeat 3` or more and look at the `spread` rows: a difference
between versions that is smaller than their spreads on that metric is noise. The judge-sanity cases are
the control: if a wrong or unsupported canned answer scores high on faithfulness or relevancy, the judge
is not discriminating and its scores say nothing about the versions either.

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
