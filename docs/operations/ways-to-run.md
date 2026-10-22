---
type: Playbook
title: Ways to run Nexo
description: The ways Nexo runs - containers or a native core, one machine or two, Ollama, vLLM or Claude per role, the CLI on the host or in a container, and the servers that sit beside it - with the command that starts each.
resource: ../../src/nexo/setup/schema.py
tags: [operations, setup, native, compose, mcp, claude-code, vllm]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-22T18:00:00Z }
sources:
  - id: schema
    resource: ../../src/nexo/setup/schema.py
    title: The setup file schema
  - id: claude-cli
    resource: ../../src/nexo/claude_cli.py
    title: Claude Code CLI model client
  - id: adr-0020
    resource: ../decisions/adr-0020-native-target-mode.md
    title: "ADR-0020: Native target mode"
---

Three choices make up a run: **where the services run** (`target` in `nexo.yaml`), **who serves each
model role** (`models`), and **how you talk to Nexo** (CLI, MCP, review pages). They are independent:
any target works with any model serving and any interface. Flags for every command are in the
[command line reference](cli.md); memory per combination is in [Choosing a setup](decision-guide.md).

# Where the services run

| Way | `target` | Presets | Runs where | Start |
|---|---|---|---|---|
| Containers | `mode: compose` (default) | `mac-single`, `linux-cpu`, `linux-gpu` | Neo4j, collector, SigNoz and add-ons in Podman or Docker; Ollama native; the Nexo CLI from the `.venv` | `make env PRESET=mac-single && make up` |
| Native core | `mode: native` | `mac-native`, `linux-native` | Neo4j as a Homebrew or system service, Ollama and Nexo on the host; only telemetry and add-ons in containers, and `telemetry: { enabled: false }` removes those too | `make env PRESET=mac-native && make neo4j-init && brew services start neo4j && make up` |
| Two machines | `mode: compose` with `models.remote` | `mac-two-machines` | Heavy roles (agent, extraction) on the remote machine's Ollama through an SSH tunnel on `localhost:11435`; light roles here | remote: `deploy/local/ollama-setup.sh m1-max-64 heavy`; here: `ssh -N -L 11435:127.0.0.1:11434 you@remote` then `make up` |
| GPU server | `mode: compose`, provider `openai-compatible` | `linux-vllm` | vLLM on 4 x 80 GB GPUs in the compose stack | `make env PRESET=linux-vllm && make up` |

On macOS, `target.machine` names the Podman machine the containers run in; `make up SWITCH=1` stops
whichever machine is running and starts that one.[^schema] Guides: [Local on Apple Silicon](local-apple-silicon.md),
[Local on Linux](local-linux.md), [Compose stack with vLLM](local-stack.md),
[ADR-0020](../decisions/adr-0020-native-target-mode.md).[^adr-0020]

Whichever target you choose, `make check` says what is missing and `nexo models --check` confirms
each role's server.

# Who serves each model

Each of the five roles (agent, extraction, router, embeddings, judge) has its own server, so they mix:

| Server | How | Notes |
|---|---|---|
| Ollama (local) | `models.local.profile`, detected by `make env` | The default on a Mac or Linux box. |
| Ollama (another machine) | `models.remote.profile`, or `roles.<role>.baseUrl` | Two profiles split heavy and light roles; a base URL moves one role. |
| vLLM or any OpenAI-compatible server | provider `openai-compatible` with `models.roles` | `linux-vllm` serves Llama 3.3 70B. |
| Claude through the Claude Code CLI | `<ROLE>_BASE_URL=claude-cli://`, `<ROLE>_MODEL=claude-sonnet-5-5` | Any role but embeddings. Uses the signed-in `claude` user, no API key. JSON roles call `claude -p --json-schema`; the agent runs `claude -p` over the read-only `nexo mcp` tools at the caller's clearance, and Nexo reads its event stream, so compliance and review are unchanged. Text sent to a Claude role leaves the machine.[^claude-cli] |

[Swapping and adding models](models.md#use-claude-for-a-role) shows each swap; `nexo models` prints
the result.

# How you use it

| Way | Command | When |
|---|---|---|
| CLI on the host | `nexo ingest`, `nexo ask`, `nexo review` (or `make ingest`, `make ask`) | The normal path; JSON on stdout. |
| CLI in a container | `make app ARGS="ask 'How is A connected to B?'"` | No local Python; runs the `nexo` image from the rendered stack (host networking on Linux). |
| Claude Code, this checkout | open Claude Code in the repo; `.mcp.json` starts `nexo mcp` over stdio | Query the graph and chunks from Claude Code, with the `nexo-knowledge` skill. |
| Claude Code, every project | `uv run nexo mcp install --scope user [--skill]` | Registers the server with absolute paths and `--directory`, so it loads this checkout's settings from anywhere. Review tools are off at user scope. |
| Other MCP clients over HTTP | `nexo mcp --transport streamable-http` | Binds `127.0.0.1:8765` without authentication; put an authenticating proxy in front before exposing it. |
| Review in a browser | `make up AGENT_INBOX=1` then `make review-inbox`, or `nexo review-ui` | Agent Inbox is the opt-in review page; `review-ui` is a loopback-only demo. |
| Watch the graph | `nexo visualize --live` (`make visualize LIVE=1`) | Serves the graph on `127.0.0.1:8767` and applies changes every 5 s, e.g. while an ingest runs. `--freeze` keeps nodes still once the layout settles. |

See [Use from Claude Code](claude-code.md), [Human review](human-review.md),
[Review in Agent Inbox](agent-inbox.md) and [Viewing the graph](../components/graph-store.md#viewing-the-graph).

# Shaping what gets ingested

Optional files, each pointed to by a setting, change ingestion on any target; keep a deployment's
own copies in git-ignored `local/` or `*.local.yaml`:

| Setting | Example | Does |
|---|---|---|
| `INGEST_EXCLUSIONS_PATH` | `deploy/ingest-exclusions.example.yaml` | Keep lines, regions or entity names out of the document, search or extraction stage, per doc type. [Ingest exclusions](../components/ingest-exclusions.md) |
| `ENTITY_TYPES_PATH` | `deploy/entity-types.example.yaml` | Choose the built-in entity types to extract and add your own, each with a description. [Triple extraction](../components/triple-extraction.md) |
| `CANONICAL_NAMES_PATH` | `deploy/canonical-names.example.yaml` | One name, type and aliases per entity; mentions matching any are rewritten first. `nexo entities merge` adds to it. [Entity resolution](../components/entity-resolution.md#canonical-names-and-merging) |
| `AMOUNT_ENTITIES` | `convert` (default), `drop`, `keep` | Quantities named as entities become attributes on the entity they describe. |
| `QUERY_EXPANSIONS` | `2` | Not ingestion: alternative phrasings the router writes for each question, fused into the seed search. |

All settings: [Configuration](configuration.md).

[^schema]: The setup file schema
[^adr-0020]: ADR-0020: Native target mode
[^claude-cli]: Claude Code CLI model client
