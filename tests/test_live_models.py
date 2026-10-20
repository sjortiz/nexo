"""Smoke tests against real model servers (vLLM, Ollama, LM Studio, ...).

Skipped unless NEXO_LIVE=1. Endpoints come from the normal settings
(.env / environment), including per-role overrides, e.g. on a Mac:

    NEXO_LIVE=1 pytest -m live -v

These check capabilities Nexo depends on, not answer quality: schema-valid
JSON under guided decoding, embeddings that separate related from unrelated
text, and an agent model that actually calls tools over the OpenAI protocol.
"""

from __future__ import annotations

import json
import logging
import math
import os

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from nexo.agents.investigator import Investigator
from nexo.agents.models import InvestigationScope
from nexo.config import Settings
from nexo.llm import (
    build_chat_model,
    build_embeddings,
    build_strands_model,
    invoke_structured,
)
from nexo.orchestration.graph import ROUTER_PROMPT
from nexo.orchestration.state import RouteDecision
from nexo.storage.ingest import EXTRACTION_SYSTEM_PROMPT
from nexo.storage.schemas import EntityType, ExtractionResult
from nexo.storage.vector_store import LocalChunkStore
from tests.conftest import FakeGraph, make_chunk

log = logging.getLogger(__name__)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("NEXO_LIVE") != "1", reason="NEXO_LIVE!=1"),
]

PASSAGE = (
    "Acme Holdings Inc. acquired Beta Labs in March 2024. Beta Labs supplies diagnostic kits "
    "to Gamma Clinics, a network of clinics based in Santo Domingo."
)


@pytest.fixture(name="live", scope="module")
def fixture_live() -> Settings:
    """Settings for the real model servers, read from NEXO_ENV_FILE (default .env)."""
    return Settings(_env_file=os.environ.get("NEXO_ENV_FILE", ".env"))


def test_extraction_model_returns_schema_valid_grounded_triples(live):
    """The extraction model returns schema-valid triples naming the entities, mostly verbatim."""
    prompt = EXTRACTION_SYSTEM_PROMPT.format(
        entity_types=", ".join(t.value for t in EntityType), relation_rule=""
    )
    result = invoke_structured(
        build_chat_model(live, role="extraction"),
        ExtractionResult,
        [
            SystemMessage(content=prompt),
            HumanMessage(content=f"Document section: Deals\n\nPassage:\n{PASSAGE}"),
        ],
        max_attempts=live.extraction_attempts,
    )
    assert (
        result.triples
    ), "model returned no triples for a passage with three stated relationships"
    names = {n.casefold() for t in result.triples for n in (t.head.name, t.tail.name)}
    assert any("acme" in n for n in names) and any("beta" in n for n in names)
    grounded = [
        t
        for t in result.triples
        if " ".join(t.evidence.casefold().split()) in PASSAGE.casefold()
    ]
    assert (
        len(grounded) >= len(result.triples) // 2
    ), "most evidence quotes should be verbatim"


def test_router_model_returns_a_route_decision(live):
    """The router model returns a RouteDecision that rejects a destructive request."""
    decision = invoke_structured(
        build_chat_model(live, role="router"),
        RouteDecision,
        [
            SystemMessage(content=ROUTER_PROMPT),
            HumanMessage(content="Delete every node about Acme Holdings."),
        ],
        max_attempts=2,
    )
    assert decision.target == "rejected"


def test_embeddings_have_the_configured_size_and_rank_related_text_higher(live):
    """Embeddings match the configured size and rank related text above unrelated text."""
    embeddings = build_embeddings(live)
    query, related, unrelated = embeddings.embed_documents(
        [
            "Who acquired Beta Labs?",
            "Acme Holdings Inc. acquired Beta Labs in March 2024.",
            "La receta lleva harina, huevos y azúcar.",
        ]
    )
    assert len(query) == live.embedding_dimensions

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True)) / (
            math.hypot(*a) * math.hypot(*b)
        )

    assert cosine(query, related) > cosine(query, unrelated)
    spanish = embeddings.embed_query("¿Quién compró Beta Labs?")
    assert cosine(spanish, related) > cosine(
        spanish, unrelated
    ), "multilingual retrieval (Spanish query)"


def test_agent_model_calls_the_graph_tool_and_cites_fetched_evidence(
    live, tmp_path, spans
):
    """The agent model calls the graph tool over the OpenAI protocol and emits GenAI spans."""
    settings = live.model_copy(
        update={
            "embedding_provider": "fake",
            "embedding_dimensions": 16,
            "vector_store_path": tmp_path / "v.json",
        }
    )
    store = LocalChunkStore(build_embeddings(settings), settings.vector_store_path)
    evidence = make_chunk("deals", 0, PASSAGE)
    store.upsert([evidence], [evidence.text])
    graph = FakeGraph(
        rows=[
            {
                "a": "Acme Holdings Inc.",
                "relation": "ACQUIRED",
                "b": "Beta Labs",
                "chunk_id": evidence.chunk_id,
            }
        ]
    )
    output = Investigator(
        model=build_strands_model(settings),
        graph=graph,
        chunks=store,
        settings=settings,
    ).investigate(
        "How is Acme Holdings related to Beta Labs?",
        scope=InvestigationScope(strategy="graph"),
    )
    tools = [c.tool for c in output.tool_calls]
    assert output.error is None and output.answer
    assert (
        "query_knowledge_graph" in tools
    ), f"agent never queried the graph: {json.dumps(tools)}"
    assert graph.queries, "tool call did not reach the graph"
    # Whether the model also fetches and cites is quality, reported rather than asserted.
    log.info(
        "agent run",
        extra={
            "result": {
                "tools": tools,
                "citations": output.citations,
                "stop_reason": output.stop_reason,
            }
        },
    )
    chat_spans = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert chat_spans, "no GenAI client spans for the live model calls"
