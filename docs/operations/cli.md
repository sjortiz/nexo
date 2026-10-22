---
type: Reference
title: Command line
description: Every nexo command and flag, its make shortcut, and what it prints; nexo --help and nexo <command> --help say the same in short.
resource: ../../src/nexo/cli.py
tags: [operations, cli, reference, make]
status: draft
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-22T18:00:00Z }
sources:
  - id: cli
    resource: ../../src/nexo/cli.py
    title: nexo CLI
  - id: setup-cli
    resource: ../../src/nexo/setup/commands.py
    title: nexo setup
  - id: mcp-install
    resource: ../../src/nexo/mcp_install.py
    title: nexo mcp install
---

# Conventions

Run commands from the checkout with the `.venv` active (`source .venv/bin/activate`), or prefix them
with `uv run`. `make <target>` wraps the common ones; `make` alone lists every target.

- **Output**: a command's result goes to stdout as JSON; logs go to stderr with trace ids
  (`LOG_FORMAT=json` for log pipelines). `nexo mcp` over stdio uses stdout for JSON-RPC.
- **Settings**: every command reads the effective settings (environment, secrets provider,
  `.nexo/render/nexo.env`, `.env`), the same ones `nexo config --explain` shows. If `nexo.yaml` changed
  since the last render, every command except `setup` warns that settings are stale.[^cli]
- **Clearance**: one of `public`, `internal`, `pii`, `phi`, lowest to highest
  ([Access levels](access-levels.md)).
- **Exit codes**: `0` success; `1` a partial failure the output describes (failed chunks, a failed
  model check); `2` a usage error or an unsupported file.

# Documents and questions

| Command | Flags | Does |
|---|---|---|
| `nexo ingest FILE` | `--source-id` (default: file stem), `--title`, `--doc-type` (default `general`), `--source-uri`, `--effective-date YYYY-MM-DD`, `--sensitivity` (default `internal`) | Chunk, classify, embed and extract a document and replace its subgraph. `.md`, `.markdown`, `.txt` pass through; `.pdf`, `.docx`, `.html` need `make install-formats` (`INGEST_CONVERTER`). Re-ingesting the same `--source-id` replaces it. [Ingestion pipeline](../components/ingestion-pipeline.md) |
| `nexo ask "QUESTION"` | `--thread-id`, `--clearance`, `--user` (default `NEXO_USER` or the login name) | Run router → investigator → compliance. Pauses for review per `spec.review`. [Request lifecycle](../architecture/request-lifecycle.md) |
| `nexo status THREAD_ID` | | A thread's state: `awaiting_review`, `completed`, `approval_status`, `outcome`. |
| `nexo review THREAD_ID` | `--approve` or `--reject` (one required), `--reviewer` (required), `--comment`, `--acknowledge-findings` | Resume a paused thread. Approving blocking findings needs `--acknowledge-findings`. [Human review](human-review.md) |
| `nexo delete SOURCE_ID` | | Remove a document and what only it supported; operators in `DELETE_OPERATORS` only. [Deleting a source](delete.md) |

Make: `make ingest FILE=doc.md DOC_TYPE=memo`, `make ask Q="..."`.

# Looking at the corpus

| Command | Flags | Does |
|---|---|---|
| `nexo visualize` | `--out` (default `graph.html`), `--open`, `--live`, `--host` (default `127.0.0.1`), `--port` (default `8767`), `--interval` seconds (default `5`), `--freeze` | Write the knowledge graph as one interactive HTML page. `--live` serves it on loopback instead and applies only what changed every `--interval`, so you can watch an ingest fill it in. `--freeze` stops the layout once it settles; the page's Pause layout button toggles it. [Viewing the graph](../components/graph-store.md#viewing-the-graph) |
| `nexo access` | `--source ID` (default: all documents) | Per document: chunk levels, the detector reasons that raised them, and what each clearance sees (visible / masked / withheld). [Access levels](access-levels.md) |
| `nexo entities duplicates` | | Names stored under more than one entity type, with mentions, relations and documents per entity. |
| `nexo entities merge --into KEY KEY...` | `--no-record` | Move mentions and relations onto the kept entity (keys look like `Organization:acme holdings`), delete the others, and record the merge in `CANONICAL_NAMES_PATH` so re-ingesting keeps it. [Canonical names and merging](../components/entity-resolution.md#canonical-names-and-merging) |
| `nexo chunks status` | | Count graph chunks that have no text. |
| `nexo chunks import [PATH]` | `PATH` default `VECTOR_STORE_PATH` | Copy a local JSON chunk store into Neo4j. [Chunk store](../components/chunk-store.md) |
| `nexo graph` | `--output FILE` | The LangGraph workflow as Mermaid; needs no models or database. |

Make: `make visualize [OUT=graph.html] [OPEN=1] [LIVE=1] [FREEZE=1]`, `make graph`.

# Models and settings

| Command | Flags | Does |
|---|---|---|
| `nexo models` | `--check` | The model and server each role uses after profiles and explicit settings. `--check` asks each server whether it serves the model (including `claude-cli://` roles) and warns when Ollama runs a model with less context than its profile sets. [Swapping and adding models](models.md) |
| `nexo config` | `--explain`, `--all` | The effective settings, secrets redacted. `--explain` names each value's source; `--all` includes defaults. [Configuration](configuration.md) |

# Setup and the stack

`nexo setup [-f FILE] VERB` works on `nexo.yaml` (or `-f`). Verbs that take `--addon` (`langfuse`,
`jaeger`, `langsmith`, `hostMetrics`, `agentInbox`) enable it for that run only.[^setup-cli]

| Verb | Flags | Does | Make |
|---|---|---|---|
| `init` | `--preset`, `--profile`, `--force` | Write `nexo.yaml` from a preset in `examples/setups/`; the profile is detected from the chip and memory. | `make env PRESET=... [PROFILE=...]` |
| `secrets` | | Create missing secrets in the provider for the enabled components. | part of `make env` |
| `lock` | `--upgrade` | Resolve `nexo.yaml` into `nexo.lock`. | `make lock [UPGRADE=1]` |
| `render` | `--addon`, `--portable` | Write `.nexo/render/`; `--portable` writes `docker-compose.yml` and `deploy/rendered/`. | `make render`, `make compose-file` |
| `check` | `--addon`, `--no-models`, `--force` | Preflight (engine, secrets, memory, ports, policies), then each role's server and model. | `make check` |
| `up` | `--addon`, `--skip-models`, `--switch-machine`, `--force` | Start Ollama's models and the stack. `--switch-machine` stops the running Podman machine to start `target.machine`. | `make up [LANGFUSE=1 JAEGER=1 LANGSMITH=1 AGENT_INBOX=1] [FORCE=1] [SWITCH=1]` |
| `down` | | Stop the stack; volumes are kept. | `make down` |
| `neo4j-init` | | `target.mode: native`: give a new Neo4j the stored password before its first start. | `make neo4j-init` |
| `compose ARGS...` | | Run compose on the rendered stack. | `make app ARGS="..."` runs `compose run --rm nexo ARGS` |

Other make targets for the stack: `make models [PROFILE=... ROLES=heavy] [APPLY=1]` pulls and starts
a profile's models without restarting anything; `make mac-metrics` / `make mac-metrics-down` run a
native collector for the Mac's memory, swap and Ollama. [How local setup works](local-setup.md)

# Servers

All of these bind loopback by default and have no authentication of their own.

| Command | Flags | Serves | Make |
|---|---|---|---|
| `nexo mcp` | `--transport stdio\|streamable-http` (default `stdio`), `--host` (default `127.0.0.1`), `--port` (default `8765`), `--directory DIR` | Seven read-only retrieval tools, plus three review tools unless `MCP_REVIEW_TOOLS=false`. `--directory` loads `.env`, rendered settings and relative data from `DIR`. [MCP server](../components/mcp-server.md) | `make mcp` |
| `nexo review-ui` | `--host`, `--port` (default `8766`) | The demo review page. | `make review-ui` |
| `nexo review-inbox` | `--port` (`REVIEW_INBOX_PORT`), `--inbox-port` (`AGENT_INBOX_PORT`) | The Agent Inbox adapter. [Review in Agent Inbox](agent-inbox.md) | `make review-inbox` |
| `nexo visualize --live` | see above | The live graph page on `8767`. | `make visualize LIVE=1` |

# Registering with Claude Code

`nexo mcp install` and `nexo mcp uninstall` add or remove Nexo in Claude Code.[^mcp-install]

| Flag | Applies to | Does |
|---|---|---|
| `--scope project\|user` | both | `project`: this checkout's `.mcp.json`. `user`: every Claude Code project. Asked when omitted; required without a terminal. |
| `--on-existing update\|skip` | both | What to do with an existing registration; otherwise asked, or skipped without a terminal. |
| `--skill` / `--no-skill` | install | Install the `nexo-knowledge` skill too (default yes for user scope). |
| `--directory DIR` | install | The settings directory the registered server loads (default: the checkout). |
| `--clearance LEVEL` | install | Pin `MCP_CLEARANCE` instead of reading it from `--directory`'s settings. |

[Use from Claude Code](claude-code.md#use-nexo-in-every-project)

# Evaluation

| Command | Flags | Does |
|---|---|---|
| `nexo eval DATASET.jsonl` | `--repeat N`, `--dataset-name`, `--run-name` | Run the investigator over a golden set (no router, compliance or review) and print JSONL scores. `--repeat` adds a mean/spread row per question. With the Langfuse add-on each run is also a Langfuse experiment, named by `--dataset-name` / `--run-name`. |
| `nexo eval-extraction DATASET.jsonl` | | Score triple extraction against gold triples; writes nothing to the stores. |

[Evaluation playbook](evaluation.md). Tests and lint: `make test`, `make test-neo4j`, `make test-live`,
`make lint`, `make ci` ([Testing](testing.md)).

[^cli]: nexo CLI
[^setup-cli]: nexo setup
[^mcp-install]: nexo mcp install
