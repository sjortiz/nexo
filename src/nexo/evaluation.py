"""Offline evaluation, following the AWS healthcare RAG guidance.

* Extraction: precision, recall and capture rate (recall penalised for
  false values) against gold triples.
* Retrieval: context precision/recall of fetched chunks; citation precision.
* Generation: LLM-as-a-judge faithfulness, relevancy and pairwise comparison
  (A/B swapped to cancel position bias). Use a judge model different from the
  generator where possible.

Metrics are also written as attributes on the current OTEL span.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from opentelemetry import trace
from pydantic import BaseModel, Field

from nexo.agents.models import AgentOutput
from nexo.llm import invoke_structured
from nexo.storage.ontology import Ontology
from nexo.storage.schemas import GroundedTriple, Triple


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


TripleKey = tuple[str, str, str]


def triple_key(triple: Triple, ontology: Ontology | None = None) -> TripleKey:
    """(head key, relation, tail key), mapped onto the ontology when one is given."""
    if ontology is None:
        return triple.head.key, triple.relation, triple.tail.key
    mapping = ontology.map(triple.relation, triple.head.type, triple.tail.type)
    head, tail = (
        (triple.tail, triple.head) if mapping.swap else (triple.head, triple.tail)
    )
    return head.key, mapping.relation, tail.key


def grounded_key(triple: GroundedTriple) -> TripleKey:
    """(head key, relation, tail key) of a triple already written to the graph."""
    return triple.head.key, triple.relation, triple.tail.key


def extraction_scores(
    predicted: Iterable[TripleKey], gold: Iterable[TripleKey]
) -> dict[str, float | None]:
    """Score predicted triples against gold: precision, recall and capture rate."""
    gold_keys = set(gold)
    pred_keys = set(predicted)
    true_positive = len(gold_keys & pred_keys)
    # A false value: right head + predicate, wrong tail (e.g. wrong dosage/date entity).
    # Keys may carry a case index in front (the overall row), so slice from the end.
    gold_slots = {k[:-1] for k in gold_keys}
    false_values = len({k for k in pred_keys - gold_keys if k[:-1] in gold_slots})
    return {
        "extraction.precision": _ratio(true_positive, len(pred_keys)),
        "extraction.recall": _ratio(true_positive, len(gold_keys)),
        "extraction.capture_rate": _ratio(
            max(true_positive - false_values, 0), len(gold_keys)
        ),
    }


def retrieval_scores(
    output: AgentOutput, relevant_chunk_ids: Iterable[str]
) -> dict[str, float | None]:
    """Score fetched and cited chunks against the relevant chunk ids."""
    relevant = set(relevant_chunk_ids)
    fetched = set(output.fetched_chunk_ids)
    cited = set(output.citations)
    return {
        "retrieval.context_precision": _ratio(len(fetched & relevant), len(fetched)),
        "retrieval.context_recall": _ratio(len(fetched & relevant), len(relevant)),
        "generation.citation_precision": _ratio(len(cited & relevant), len(cited)),
    }


def answer_scores(
    answer: str, answer_contains: Iterable[str]
) -> dict[str, float | None]:
    """Share of the expected phrases the answer contains (case-insensitive).

    A check that needs no judge model: put the facts a correct answer must state in the
    golden case's `answer_contains`.
    """
    expected = [phrase.lower() for phrase in answer_contains]
    found = sum(phrase in answer.lower() for phrase in expected)
    return {"generation.answer_contains": _ratio(found, len(expected))}


class JudgeScore(BaseModel):
    """A judge's 1-5 score with its rationale and any unsupported claims."""

    score: int = Field(ge=1, le=5)
    rationale: str = Field(max_length=1000)
    unsupported_claims: list[str] = Field(default_factory=list)


class PairwiseVerdict(BaseModel):
    """A judge's pick between two answers."""

    winner: Literal["A", "B", "tie"]
    rationale: str = Field(max_length=1000)


_FAITHFULNESS = (
    "Rate 1-5 how fully the ANSWER is supported by the EVIDENCE alone (5 = every claim "
    "supported, 1 = mostly unsupported). List each unsupported claim verbatim. Ignore style."
)
_RELEVANCY = (
    "Rate 1-5 how directly the ANSWER addresses the QUESTION (5 = complete and on point). "
    "Do not judge factual accuracy."
)
_PAIRWISE = (
    "Compare two answers to the QUESTION{reference_hint}. Prefer the one that is more "
    'accurate, better supported and more complete. Answer "tie" if equivalent. '
    "Ignore length and order."
)


def judge_faithfulness(
    judge: BaseChatModel, answer: str, evidence: Sequence[str]
) -> JudgeScore:
    """Ask the judge how well the evidence alone supports the answer."""
    body = "\n\n".join(f"<evidence>\n{e}\n</evidence>" for e in evidence)
    return invoke_structured(
        judge,
        JudgeScore,
        [
            SystemMessage(content=_FAITHFULNESS),
            HumanMessage(content=f"{body}\n\nANSWER:\n{answer}"),
        ],
    )


def judge_relevancy(judge: BaseChatModel, question: str, answer: str) -> JudgeScore:
    """Ask the judge how directly the answer addresses the question."""
    return invoke_structured(
        judge,
        JudgeScore,
        [
            SystemMessage(content=_RELEVANCY),
            HumanMessage(content=f"QUESTION:\n{question}\n\nANSWER:\n{answer}"),
        ],
    )


def judge_pairwise(
    judge: BaseChatModel,
    question: str,
    answer_a: str,
    answer_b: str,
    reference: str | None = None,
) -> Literal["A", "B", "tie"]:
    """Run both orders; disagreement between orders counts as a tie."""
    system = _PAIRWISE.format(
        reference_hint=" against the REFERENCE answer" if reference else ""
    )
    ref = f"REFERENCE:\n{reference}\n\n" if reference else ""

    def ask(first: str, second: str) -> str:
        verdict = invoke_structured(
            judge,
            PairwiseVerdict,
            [
                SystemMessage(content=system),
                HumanMessage(
                    content=f"QUESTION:\n{question}\n\n{ref}"
                    f"ANSWER A:\n{first}\n\nANSWER B:\n{second}"
                ),
            ],
        )
        return verdict.winner

    forward = ask(answer_a, answer_b)
    backward = {"A": "B", "B": "A", "tie": "tie"}[ask(answer_b, answer_a)]
    return forward if forward == backward else "tie"


def record_metrics(metrics: dict[str, float | None]) -> dict[str, float | None]:
    """Set the non-null metrics as eval.* attributes on the current span."""
    span = trace.get_current_span()
    span.set_attributes({f"eval.{k}": v for k, v in metrics.items() if v is not None})
    return metrics
