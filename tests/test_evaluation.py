"""Evaluation scores that need no model."""

from __future__ import annotations

from nexo.evaluation import answer_scores


def test_answer_scores_counts_expected_phrases_case_insensitively():
    """Each expected phrase found counts; a wrong answer scores lower."""
    chain = ["Beta Labs", "Jane Roe", "Delta Devices"]
    good = "Acme bought BETA LABS; jane roe runs it and led product at Delta Devices."
    assert answer_scores(good, chain) == {"generation.answer_contains": 1.0}
    wrong = "Delta Devices does not exist in the knowledge graph."
    assert answer_scores(wrong, chain) == {"generation.answer_contains": 0.3333}


def test_answer_scores_without_expectations_is_unscored():
    """No expected phrases means no score rather than a perfect one."""
    assert answer_scores("anything", []) == {"generation.answer_contains": None}
