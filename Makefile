# Nexo tasks. `make` lists them.
#
# uv installs Python and the dependencies into .venv; the tasks run tools from there.
# Works with GNU Make 3.81 (macOS) and 4.x (Linux).

VENV      ?= .venv
RUN       ?= $(VENV)/bin/
CONTAINER ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)
COMPOSE   ?= $(CONTAINER) compose
# On Linux the nexo container uses the host network to reach Ollama (see docker-compose.linux.yml).
ifeq ($(shell uname -s),Linux)
COMPOSE_APP ?= $(COMPOSE) -f docker-compose.yml -f docker-compose.linux.yml --profile app
else
COMPOSE_APP ?= $(COMPOSE) --profile app
endif

# Opt-in observability add-ons for `make up`: JAEGER=1, LANGSMITH=1.
JAEGER    ?=
LANGSMITH ?=
ADDONS    = OTEL_JAEGER=$(if $(JAEGER),jaeger,none) OTEL_LANGSMITH=$(if $(LANGSMITH),langsmith,none)
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
.PHONY: help install install-notebook env models up down check ingest ask mcp graph app \
        lint format test neo4j-test test-neo4j neo4j-test-down test-live ci \
        walkthrough site serve-site notebook clean

help: ## List the targets
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n%s\n", substr($$0, 5)} /^[a-z0-9-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

##@ Set up

install: ## Install Python, Nexo and the dev tools into .venv with uv (from uv.lock)
	uv sync

install-notebook: ## Also install Jupyter, for the walkthrough notebook
	uv sync --extra notebook

env: ## Create .env from the local template and fill missing secrets (keeps existing values)
	@if [ -f .env ]; then echo ".env exists; keeping its values"; else cp deploy/local/local.env.example .env && echo "created .env: set LOCAL_PROFILE"; fi
	@deploy/local/env-secrets.sh .env

models: ## Pull and start the profile's models in Ollama (PROFILE=m1-max-64 ROLES=heavy; default: .env)
	deploy/local/ollama-setup.sh $(PROFILE) $(ROLES)

##@ Run

up: ## Start Neo4j, the collector and Langfuse (add-ons: JAEGER=1, LANGSMITH=1)
	$(ADDONS) $(COMPOSE) up -d neo4j otel-collector langfuse-web langfuse-worker $(if $(JAEGER),jaeger)

down: ## Stop them (data volumes are kept)
	$(COMPOSE) --profile jaeger down

check: ## Check each model role's server and model (nexo models --check)
	$(RUN)nexo models --check

ingest: ## Ingest a document: make ingest FILE=corpus/memo.md DOC_TYPE=memo
	@if [ -z "$(FILE)" ]; then echo "usage: make ingest FILE=path/to/doc.md [DOC_TYPE=memo]" >&2; exit 2; fi
	$(RUN)nexo ingest "$(FILE)" --doc-type "$(DOC_TYPE)"

ask: ## Ask a question (pauses for review): make ask Q="How is A connected to B?"
	@if [ -z "$(Q)" ]; then echo 'usage: make ask Q="your question"' >&2; exit 2; fi
	$(RUN)nexo ask "$(Q)"

mcp: ## Serve the knowledge graph to MCP clients over stdio
	$(RUN)nexo mcp

graph: ## Print the LangGraph workflow as Mermaid
	$(RUN)nexo graph

app: ## Run the nexo CLI in a container: make app ARGS="models --check"
	$(COMPOSE_APP) run --rm nexo $(ARGS)

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
