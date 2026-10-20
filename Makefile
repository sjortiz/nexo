# Nexo tasks. `make` lists them.
#
# uv installs Python and the dependencies into .venv; the tasks run tools from there.
# Works with GNU Make 3.81 (macOS) and 4.x (Linux).

VENV      ?= .venv
RUN       ?= $(VENV)/bin/
CONTAINER ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
# The stack is described by nexo.yaml (`make env` writes it from a preset) and run by
# `nexo setup`, which renders .nexo/render/. Add-ons for one run, without editing nexo.yaml:
# make up LANGFUSE=1 JAEGER=1 LANGSMITH=1 AGENT_INBOX=1.
SETUP     ?= nexo.yaml
PRESET    ?=
LANGFUSE  ?=
JAEGER    ?=
LANGSMITH ?=
AGENT_INBOX ?=
UPGRADE   ?=
FORCE     ?=
NEXO_SETUP = $(RUN)nexo setup -f $(SETUP)
ADDONS     = $(if $(LANGFUSE),--addon langfuse) $(if $(JAEGER),--addon jaeger) \
             $(if $(LANGSMITH),--addon langsmith) $(if $(AGENT_INBOX),--addon agentInbox)
PROFILE  ?=
ROLES    ?=
FILE     ?=
DOC_TYPE ?= general
Q        ?=
ARGS     ?=
PORT     ?= 8000
NEO4J_TEST_PORT     ?= 7688
NEO4J_TEST_PASSWORD ?= nexo-test-pass

.DEFAULT_GOAL := help
.PHONY: help install install-notebook install-keychain env models up down check render lock compose-file \
        ingest ask mcp review-inbox graph app \
        lint format test neo4j-test test-neo4j neo4j-test-down test-live ci \
        walkthrough site serve-site notebook clean

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-z0-9-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Set up

install: ## Install Python, Nexo and the dev tools into .venv with uv (from uv.lock)
	uv sync

install-notebook: ## Also install Jupyter, for the walkthrough notebook
	uv sync --extra notebook

install-keychain: ## Also install keyring, for secrets.provider keychain (local machines)
	uv sync --extra keychain

env: ## Write nexo.yaml from a preset (PRESET=mac-single) and create missing secrets in ~/.config/nexo/config
	$(NEXO_SETUP) init $(if $(PRESET),--preset $(PRESET))
	$(NEXO_SETUP) secrets

models: ## Pull and start the profile's models in Ollama (PROFILE=m1-max-64 ROLES=heavy; default: .env)
	deploy/local/ollama-setup.sh $(PROFILE) $(ROLES)

##@ Run

up: ## Start the models and the stack in nexo.yaml (one run: LANGFUSE=1 JAEGER=1 LANGSMITH=1 AGENT_INBOX=1; FORCE=1)
	$(NEXO_SETUP) up $(ADDONS) $(if $(FORCE),--force)

down: ## Stop the stack (data volumes are kept)
	$(NEXO_SETUP) down

check: ## Preflight (engine, secrets, memory), then each model role's server and model
	$(NEXO_SETUP) check $(ADDONS) $(if $(FORCE),--force)

render: ## Write .nexo/render/ from nexo.yaml without starting anything
	$(NEXO_SETUP) render $(ADDONS)

lock: ## Resolve nexo.yaml into nexo.lock (UPGRADE=1 moves every pin to the current defaults)
	$(NEXO_SETUP) lock $(if $(UPGRADE),--upgrade)

compose-file: ## Regenerate docker-compose.yml and deploy/rendered/ from the mac-single preset
	$(RUN)nexo setup -f examples/setups/mac-single/nexo.yaml render --portable

ingest: ## Ingest a document: make ingest FILE=corpus/memo.md DOC_TYPE=memo
	@if [ -z "$(FILE)" ]; then echo "usage: make ingest FILE=path/to/doc.md [DOC_TYPE=memo]" >&2; exit 2; fi
	$(RUN)nexo ingest "$(FILE)" --doc-type "$(DOC_TYPE)"

ask: ## Ask a question (pauses for review): make ask Q="How is A connected to B?"
	@if [ -z "$(Q)" ]; then echo 'usage: make ask Q="your question"' >&2; exit 2; fi
	$(RUN)nexo ask "$(Q)"

mcp: ## Serve the knowledge graph to MCP clients over stdio
	$(RUN)nexo mcp

review-inbox: ## Serve the Agent Inbox adapter on 127.0.0.1 (opt-in; start the page with make up AGENT_INBOX=1)
	$(RUN)nexo review-inbox

graph: ## Print the LangGraph workflow as Mermaid
	$(RUN)nexo graph

app: ## Run the nexo CLI in a container: make app ARGS="models --check"
	$(NEXO_SETUP) compose run --rm nexo $(ARGS)

##@ Test

lint: ## Check formatting with black and lint with pylint
	$(RUN)black --check src tests examples
	$(RUN)pylint src tests examples

format: ## Format the code with black
	$(RUN)black src tests examples

test: ## Run the test suite (the Neo4j tests also run when NEO4J_TEST_URI is set)
	$(RUN)pytest -o addopts="" -q -rs

neo4j-test: ## Start a throwaway Neo4j for the integration tests on 127.0.0.1:7688
	@$(CONTAINER) start nexo-neo4j-test >/dev/null 2>&1 || $(CONTAINER) run -d --name nexo-neo4j-test -p 127.0.0.1:$(NEO4J_TEST_PORT):7687 -e NEO4J_AUTH=neo4j/$(NEO4J_TEST_PASSWORD) neo4j:5.26-community >/dev/null
	@for i in $$(seq 1 60); do $(RUN)python -c "from neo4j import GraphDatabase; GraphDatabase.driver('bolt://127.0.0.1:$(NEO4J_TEST_PORT)', auth=('neo4j', '$(NEO4J_TEST_PASSWORD)')).verify_connectivity()" 2>/dev/null && echo "Neo4j ready on 127.0.0.1:$(NEO4J_TEST_PORT)" && exit 0; sleep 2; done; $(CONTAINER) logs nexo-neo4j-test; exit 1

test-neo4j: neo4j-test ## Run the Neo4j integration tests against that throwaway Neo4j
	NEO4J_TEST_URI=bolt://127.0.0.1:$(NEO4J_TEST_PORT) NEO4J_TEST_PASSWORD=$(NEO4J_TEST_PASSWORD) $(RUN)pytest -o addopts="" -q tests/test_neo4j_integration.py

neo4j-test-down: ## Remove the throwaway Neo4j
	$(CONTAINER) rm -f nexo-neo4j-test

test-live: ## Check the real models (needs them running: make models)
	NEXO_LIVE=1 $(RUN)pytest -o addopts="" -m live -v

ci: lint test ## What CI runs: lint, then tests

##@ Walkthrough and site

walkthrough: ## Re-run the walkthrough notebook (real models and Neo4j), then rebuild its page
	$(RUN)python examples/build_walkthrough.py --record

site: ## Rebuild the walkthrough page after prose-only edits (no model calls)
	$(RUN)python examples/build_walkthrough.py

serve-site: ## Preview the GitHub Pages site on http://127.0.0.1:8000
	$(RUN)python -m http.server $(PORT) --bind 127.0.0.1 --directory site

notebook: ## Open the walkthrough notebook in JupyterLab (make install-notebook first)
	$(RUN)jupyter lab examples/walkthrough.ipynb

clean: ## Remove caches and build output (keeps .venv, .env and .data)
	rm -rf .pytest_cache build dist
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -prune -exec rm -rf {} +
