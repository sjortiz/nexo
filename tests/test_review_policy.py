"""The review policy (spec.review, REVIEW_POLICY): which answers wait for a person."""

from __future__ import annotations

import pytest

from nexo.config import Settings
from nexo.orchestration.compliance import (
    AUTO_REVIEWER,
    ComplianceReport,
    Finding,
    auto_approves,
)
from nexo.setup import check
from nexo.setup.render import build_env
from nexo.setup.schema import SetupError, load_casting
from tests.test_graph import CHUNK, FakeInvestigator, _review, _state, grounded_output
from tests.test_graph import runtime as _runtime
from tests.test_setup import _render, _setup

POLICIES = ("always", "on-findings", "never")
# Cites a chunk the agent never read: ungrounded_citations, a blocking finding.
UNGROUNDED = {"fetched_sensitivity": {}, "fetched_chunk_ids": []}
# Stopped on its budget: budget_exhausted, a warning only.
WARNED = {"stop_reason": "limit_turns"}


def _with_policy(settings, policy, output):
    settings.review_policy = policy
    return _runtime(settings, FakeInvestigator([output]))


def test_always_is_the_default_and_pauses_even_a_clean_answer(settings):
    """Unset, every answer waits for a person, as before the policy existed."""
    assert settings.review_policy == "always"
    paused = _runtime(settings, FakeInvestigator([grounded_output()])).start("q")
    assert paused.status == "awaiting_review" and paused.final_response is None
    assert not _review(paused)["compliance_report"]["findings"]


def test_on_findings_approves_a_clean_grounded_answer_as_system_auto(settings, spans):
    """No findings and grounded citations: released without a pause, system:auto on record."""
    investigator = FakeInvestigator([grounded_output()])
    settings.review_policy = "on-findings"
    rt = _runtime(settings, investigator)
    done = rt.start("q", thread_id="t1")
    assert done.status == "completed" and done.approval_status == "approved"
    assert done.final_response == grounded_output().answer
    review = _state(rt, "t1")["review"]
    assert review["reviewer"] == AUTO_REVIEWER == "system:auto"
    assert review["approval_status"] == "approved"
    assert "on-findings" in review["comment"]
    assert len(investigator.calls) == 1
    compliance = next(
        s for s in spans.get_finished_spans() if s.name == "langgraph.node compliance"
    )
    assert compliance.attributes["nexo.review.reviewer"] == "system:auto"
    assert compliance.attributes["nexo.review.policy"] == "on-findings"
    assert "langgraph.interrupted" not in compliance.attributes
    workflow = next(
        s for s in spans.get_finished_spans() if s.name.startswith("invoke_workflow")
    )
    assert workflow.attributes["nexo.approval_status"] == "approved"


def test_on_findings_pauses_an_answer_with_only_warnings(settings):
    """A warning is a finding: a person decides."""
    paused = _with_policy(settings, "on-findings", grounded_output(**WARNED)).start("q")
    assert paused.status == "awaiting_review"
    codes = {f["code"] for f in _review(paused)["compliance_report"]["findings"]}
    assert codes == {"budget_exhausted"}


def test_never_approves_warnings_and_lists_them_on_the_record(settings):
    """never: an answer with warnings only is released, the warnings in the comment."""
    rt = _with_policy(settings, "never", grounded_output(**WARNED))
    done = rt.start("q", thread_id="t1")
    assert done.status == "completed" and done.approval_status == "approved"
    review = _state(rt, "t1")["review"]
    assert review["reviewer"] == "system:auto"
    assert "budget_exhausted" in review["comment"]


@pytest.mark.parametrize("policy", POLICIES)
def test_blocking_findings_always_wait_for_a_person(settings, policy):
    """Whatever the policy, never auto-approve an answer with blocking findings."""
    rt = _with_policy(settings, policy, grounded_output(**UNGROUNDED))
    paused = rt.start("q", thread_id="t1")
    assert paused.status == "awaiting_review" and paused.final_response is None
    assert _review(paused)["compliance_report"]["blocking"] is True
    # A person still needs to acknowledge the findings to approve.
    blocked = rt.review("t1", {"approval_status": "approved", "reviewer": "a"})
    assert "acknowledged_findings" in _review(blocked)["error"]


@pytest.mark.parametrize("policy", POLICIES)
def test_the_guard_holds_without_the_graph(policy):
    """auto_approves itself refuses blocking findings, even a lone one under never."""
    report = ComplianceReport(
        findings=[Finding(code="phi_in_evidence", severity="block", detail="x")]
    )
    assert not auto_approves(policy, report, grounded_output())


@pytest.mark.parametrize("policy", POLICIES)
def test_only_warnings_are_released_without_a_person(policy):
    """An allow-list: a severity other than warn (say one added later) always waits."""
    report = ComplianceReport(
        findings=[Finding.model_construct(code="new", severity="critical", detail="x")]
    )
    assert not report.blocking
    assert not auto_approves(policy, report, grounded_output())


def test_on_findings_also_requires_grounded_citations():
    """No findings is not enough on its own: every citation must be a chunk the agent read."""
    clean = ComplianceReport()
    assert auto_approves("on-findings", clean, grounded_output())
    for output in (
        grounded_output(citations=[]),
        grounded_output(fetched_sensitivity={}),
        grounded_output(citations=[CHUNK, "chk_" + "b" * 16]),
    ):
        assert not auto_approves("on-findings", clean, output)
    assert not auto_approves("always", clean, grounded_output())


def test_setup_file_sets_the_policy_and_the_default_is_rendered_too(workdir):
    """spec.review reaches nexo.env as REVIEW_POLICY, the default included."""
    _, _, env = _render(workdir, _setup(workdir, "mac-single"))
    assert env["REVIEW_POLICY"] == "always"
    path = _setup(
        workdir, "mac-single", lambda d: d["spec"].update(review="on-findings")
    )
    assert load_casting(path).spec.review == "on-findings"
    _, _, env = _render(workdir, path)
    assert env["REVIEW_POLICY"] == "on-findings"


def test_rendered_env_overrides_a_stale_policy_in_dot_env(workdir, tmp_path):
    """A REVIEW_POLICY left in .env does not relax spec.review always."""
    stale = tmp_path / "stale.env"
    stale.write_text("REVIEW_POLICY=never\n")
    for declared in POLICIES:
        path = _setup(
            workdir, "mac-single", lambda d, p=declared: d["spec"].update(review=p)
        )
        rendered = tmp_path / f"{declared}.env"
        env = build_env(load_casting(path))
        rendered.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        assert Settings(_env_file=(stale, rendered)).review_policy == declared


def test_unknown_policy_is_rejected(workdir):
    """A typo stops before anything renders."""
    with pytest.raises(SetupError, match="spec.review"):
        load_casting(
            _setup(
                workdir, "mac-single", lambda d: d["spec"].update(review="sometimes")
            )
        )


@pytest.mark.parametrize(
    "declared, environ, expected",
    [
        ("always", None, "always: every answer waits"),
        ("on-findings", None, "on-findings: answers with no findings"),
        ("never", "always", "always: every answer waits"),
        ("always", "never", "environment overrides spec.review always"),
    ],
)
def test_check_shows_the_policy_answers_run_under(
    workdir, monkeypatch, declared, environ, expected
):
    """make check names the policy, and says when the process environment overrides it."""
    path = _setup(workdir, "mac-single", lambda d: d["spec"].update(review=declared))
    if environ:
        monkeypatch.setenv("REVIEW_POLICY", environ)
    else:
        monkeypatch.delenv("REVIEW_POLICY", raising=False)
    finding = check.review_finding(load_casting(path))
    assert finding.name == "review" and finding.ok
    assert expected in finding.detail
