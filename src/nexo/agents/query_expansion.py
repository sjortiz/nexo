"""Seed query expansion: search with the document's words, not only the asker's.

A question says "vacation days" where the documents say "leave policy"; one
search with the question's wording misses them. `QUERY_EXPANSIONS` (default 2)
asks the router model for that many alternative search queries, and the seed
passages are the fused results of the question and its rewrites. Any failure
falls back to the question alone.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, ConfigDict, Field

from nexo.llm import invoke_structured

logger = logging.getLogger(__name__)

EXPANSION_PROMPT = """\
Rewrite the question as up to {n} short search queries for a company document collection. Use \
other words the documents might use for the same thing: synonyms, the business or technical term \
for an everyday phrase, related terms (for example "vacation days" -> "paid time off", "leave \
policy"). Keep names, products and dates from the question. Queries only, no answers."""


class Expansions(BaseModel):
    """Alternative search queries for one question."""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(default_factory=list, max_length=8)


Expander = Callable[[str], list[str]]


def build_expander(llm: BaseChatModel, n: int) -> Expander:
    """A function returning up to `n` rewrites of a question (none on any failure)."""

    def expand(question: str) -> list[str]:
        try:
            result = invoke_structured(
                llm,
                Expansions,
                [
                    SystemMessage(content=EXPANSION_PROMPT.format(n=n)),
                    HumanMessage(content=question),
                ],
                max_attempts=1,
            )
        except Exception:  # pylint: disable=broad-exception-caught  # optional aid
            logger.warning("query expansion failed; searching with the question only")
            return []
        seen = {question.strip().lower()}
        queries = []
        for query in result.queries:
            key = query.strip().lower()
            if key and key not in seen:
                seen.add(key)
                queries.append(query.strip())
        return queries[:n]

    return expand
