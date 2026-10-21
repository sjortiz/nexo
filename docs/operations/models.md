---
type: Playbook
title: Swapping and adding models
description: How each role gets its model, how to change one role or add a judge, how to point a role at another machine, and how to confirm the result.
resource: ../../src/nexo/local_profiles.py
tags: [operations, models, ollama]
status: draft
generated: { by: claude-code/claude-fable-5-1, at: 2026-10-21T15:00:00Z }
sources:
  - id: profiles
    resource: ../../src/nexo/local_profiles.py
    title: Profiles, tiers and role sets
  - id: schema
    resource: ../../src/nexo/setup/schema.py
    title: spec.models
  - id: config
    resource: ../../src/nexo/config.py
    title: Settings and the role endpoints
  - id: llm
    resource: ../../src/nexo/llm.py
    title: resolved_models and check_model_servers
---

# How a role gets its model

Five roles call a model: `agent`, `extraction`, `router`, `judge` and `embedding`. For each one the
answer to "which model, on which server" comes from the first of these that sets it:[^config]

1. An explicit setting for the role: `AGENT_MODEL`, `AGENT_BASE_URL`, `AGENT_REASONING_EFFORT` and the
   same for the other roles (`nexo.yaml` writes them from `spec.models.roles`).
2. The profile: `LOCAL_PROFILE` (and `REMOTE_PROFILE` for two machines) names the hardware, the
   profile picks the largest catalog tier that fits its memory, and the tier assigns a model to every
   role, or turns a role off (no judge on a small Mac).[^profiles]
3. The `VLLM_*` defaults, for a vLLM or any OpenAI-compatible server without a profile.

`nexo models` prints the result, one row per role, and `nexo models --check` asks each server whether
it lists the model and, for the JSON roles (`extraction`, `router`, `judge`), whether it can constrain
its output to a schema.[^llm] Run it after every change below; `make check` runs the same probe.

# Swap one role's model

In `nexo.yaml`, under the profile that stays:[^schema]

```yaml
spec:
  models:
    local: { profile: m1-max-64 }
    roles:
      agent: { model: gemma4:31b }
```

Then `make up` (or `make models` to pull without restarting the stack): the model is pulled into
Ollama, the other roles keep the profile's models, and `nexo models` shows `agent: gemma4:31b`.
Models that reason before answering (`THINKING_MODELS` in the catalog) get `reasoning_effort: none`
on the JSON roles automatically, because the schema is only enforced after the reasoning section.

# Add a judge

Small profiles leave the judge off, and `nexo eval` then skips the generation scores. Turn it on with
a model that fits beside the others:

```yaml
    roles:
      judge: { model: llama3.1:8b-instruct-q4_K_M }
```

The judge adds its size to the memory budget `make check` reports; prefer a different model from the
agent to limit self-preference, and read [Evaluation](evaluation.md#judge-scores) for what it scores.

# Point a role at another machine

Any role can run elsewhere. Give it a server, with a key if the server needs one:

```yaml
    roles:
      agent: { model: qwen3:30b-a3b-instruct-2507-q4_K_M, baseUrl: "http://gpu-box:11434/v1" }
```

For the common split, heavy roles on a GPU machine and light roles on the Mac, use two profiles
instead (`local` and `remote`): the tier of each machine assigns its role set, and an SSH tunnel brings
the remote server to `localhost:11435` ([Apple Silicon](local-apple-silicon.md)).

# Memory

Every catalog model has a size in `MODEL_SIZES_GB`; `make check` sums the models the lock places on
this machine and compares the total with the machine's memory, with the containers' share taken out.
If a swap does not fit, the finding says by how much; the choices are a smaller quantization, a
smaller role model, or moving a role to another machine.

# Confirm

```
$ nexo models --check
{"local_profile": "m1-max-64", "roles": [{"role": "agent", "model": "gemma4:31b", "check": "ok"}, ...]}
```

`model not served here` lists what the server has instead; `structured output failed` means the
server lists the model but cannot constrain it, which breaks extraction and routing: pick another
build of the model or another role for it.

[^config]: Settings and the role endpoints
[^profiles]: Profiles, tiers and role sets
[^schema]: spec.models
[^llm]: resolved_models and check_model_servers
