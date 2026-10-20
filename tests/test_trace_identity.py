"""Which user and which question a trace belongs to, without exporting the question."""

from __future__ import annotations

from pydantic import SecretStr

from nexo import telemetry
from nexo.telemetry import SESSION_ID, caller_attributes, query_attributes


def _with(settings, **values):
    return settings.model_copy(update=values)


def test_the_hash_matches_a_question_however_it_is_typed(settings):
    """Case and spacing do not change the hash; another question or key does."""
    keyed = _with(settings, query_hash_key=SecretStr("k1"))
    one = query_attributes(keyed, "How is Acme connected to Gamma?")
    assert (
        one["nexo.query.hash"]
        == query_attributes(keyed, "  how is ACME   connected to gamma? ")[
            "nexo.query.hash"
        ]
    )
    assert len(one["nexo.query.hash"]) == 16
    assert (
        one["nexo.query.hash"]
        != query_attributes(keyed, "Who owns Gamma?")["nexo.query.hash"]
    )
    other_key = _with(settings, query_hash_key=SecretStr("k2"))
    assert (
        one["nexo.query.hash"]
        != query_attributes(other_key, "How is Acme connected to Gamma?")[
            "nexo.query.hash"
        ]
    )
    assert one["nexo.query.length"] == len("How is Acme connected to Gamma?")


def test_no_text_no_key_by_default(settings):
    """Without the opt-in the question is not exported; without a key there is no hash."""
    question = "Patient Jane Roe's diagnosis?"
    attributes = query_attributes(
        _with(settings, query_hash_key=SecretStr("k1")), question
    )
    assert "nexo.query.text" not in attributes
    assert question not in str(attributes) and "k1" not in str(attributes)
    assert "nexo.query.hash" not in query_attributes(
        _with(settings, query_hash_key=None), question
    )


def test_text_only_when_opted_in(settings):
    """TRACE_QUERY_TEXT=true (telemetry.queryText) puts the question on the span."""
    opted = _with(settings, trace_query_text=True)
    assert (
        query_attributes(opted, "Who owns Gamma?")["nexo.query.text"]
        == "Who owns Gamma?"
    )


def test_caller_is_the_given_user_then_nexo_user_then_the_login(settings, monkeypatch):
    """user.id: --user or the reviewer, else NEXO_USER, else the login name.

    One session.id per process.
    """
    monkeypatch.setattr(telemetry.getpass, "getuser", lambda: "login-name")
    assert caller_attributes(settings, "ana")["user.id"] == "ana"
    assert caller_attributes(_with(settings, nexo_user="bo"))["user.id"] == "bo"
    assert caller_attributes(_with(settings, nexo_user=None))["user.id"] == "login-name"
    assert caller_attributes(settings)["session.id"] == SESSION_ID
