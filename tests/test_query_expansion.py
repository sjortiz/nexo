"""Seed query expansion (QUERY_EXPANSIONS)."""

from __future__ import annotations

import json

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from nexo.agents.query_expansion import build_expander


def _llm(*replies):
    return GenericFakeChatModel(messages=iter([AIMessage(content=r) for r in replies]))


def test_expander_returns_new_distinct_queries():
    """Rewrites that repeat the question or each other are dropped; n caps the rest."""
    reply = json.dumps(
        {
            "queries": [
                "Vacation days?",
                "leave policy",
                "LEAVE POLICY",
                "paid time off",
                "x",
            ]
        }
    )
    expand = build_expander(_llm(reply), 2)
    assert expand("vacation days?") == ["leave policy", "paid time off"]


def test_expander_failure_falls_back_to_the_question():
    """Invalid model output means no rewrites, never an error."""
    assert build_expander(_llm("not json"), 2)("q") == []
