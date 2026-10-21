"""Question and answer logs (telemetry.content): exported by the run's sensitivity (ADR-0009)."""

from __future__ import annotations

import io
import logging
from contextlib import contextmanager

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)

from nexo.config import Settings
from nexo.logging_setup import CONTENT_LOGGER, JsonFormatter, configure_logging
from nexo.service import content_gate, run_sensitivity
from nexo.setup import check
from nexo.setup.render import build_env
from nexo.setup.schema import SetupError, load_casting
from nexo.storage.schemas import Sensitivity
from nexo.storage.sensitive_data import NoopDetector, RegexDetector
from nexo.telemetry import OtelLogHandler
from tests.test_graph import CHUNK, FakeInvestigator, grounded_output, route, runtime
from tests.test_setup import _render, _setup

QUESTION = "How is Acme related to Beta?"
ANSWER = grounded_output().answer
THRESHOLDS = ("none", "public", "internal", "pii", "phi")
LEVELS = ("public", "internal", "pii", "phi")
WORKFLOW = "invoke_workflow nexo-investigation"


class BrokenDetector:  # pylint: disable=too-few-public-methods
    """A detector whose service is down."""

    def detect(self, text):
        """Fail like an unreachable Comprehend Medical."""
        raise ConnectionError(f"detector unreachable ({len(text)} chars)")


@contextmanager
def exported_logs():
    """Records as the OTLP handler exports them, wired as the CLI wires logging.

    LOG_LEVEL=WARNING: the content record must still reach OTLP. The stderr text is
    kept on `exporter.stderr`.
    """
    exporter = InMemoryLogRecordExporter()
    provider = LoggerProvider()
    provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
    root, content = logging.getLogger(), logging.getLogger(CONTENT_LOGGER)
    saved = root.handlers[:], root.level, content.level
    exporter.stderr = io.StringIO()
    try:
        configure_logging(
            Settings(_env_file=None, log_level="WARNING"), exporter.stderr
        )
        root.addHandler(OtelLogHandler(provider, capture_content=False))
        yield exporter
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        content.setLevel(saved[2])


def _content_records(exporter):
    return [
        (str(log.log_record.body), dict(log.log_record.attributes or {}))
        for log in exporter.get_finished_logs()
        if "content:" in str(log.log_record.body)
    ]


def _content_events(spans):
    return [
        dict(event.attributes or {})
        for span in spans.get_finished_spans()
        if span.name == WORKFLOW
        for event in span.events
        if event.name == "nexo.content"
    ]


def _answered(settings, level, question=QUESTION, **review):
    """Run a question over one chunk of `level`, approve it, and return the review's thread."""
    rt = runtime(
        settings,
        FakeInvestigator([grounded_output(fetched_sensitivity={CHUNK: level})]),
    )
    rt.start(question, thread_id="c1")
    decision = {
        "approval_status": "approved",
        "reviewer": "ana",
        "acknowledged_findings": True,
    }
    return rt.review("c1", decision | review)


@pytest.mark.parametrize("threshold", THRESHOLDS)
@pytest.mark.parametrize("level", LEVELS)
def test_each_threshold_against_runs_of_each_sensitivity(
    settings, spans, threshold, level
):
    """Released at or below the threshold (log record and span event), withheld above it."""
    settings.telemetry_content_max_sensitivity = threshold
    with exported_logs() as exporter:
        done = _answered(settings, level, comment="checked")
    assert done.final_response == ANSWER
    (start, start_attrs), (review, attrs) = _content_records(exporter)
    released = threshold != "none" and (
        Sensitivity(level).rank <= Sensitivity(threshold).rank
    )
    events = _content_events(spans)
    if released:
        assert review == f"thread c1: content: released ({level})"
        assert attrs["nexo.content.question"] == QUESTION
        assert attrs["nexo.content.answer"] == ANSWER
        assert attrs["nexo.content.review_comment"] == "checked"
        assert start_attrs["nexo.content.question"] == QUESTION
        assert "nexo.content.answer" not in start_attrs  # a draft is never exported
        assert events[-1]["nexo.content.answer"] == ANSWER
    else:
        reason = "telemetry content none" if threshold == "none" else level
        assert review == f"thread c1: content: withheld ({reason})"
        assert start == f"thread c1: content: withheld ({reason})"
        exported = f"{start_attrs} {attrs} {events}"
        assert QUESTION not in exported and "Acme acquired" not in exported
        assert not events
    verdicts = [
        s.attributes["nexo.content"]
        for s in spans.get_finished_spans()
        if s.name == WORKFLOW
    ]
    assert verdicts[-1] == review.split("content: ", 1)[1]


def test_personal_details_in_the_question_withhold_it_without_a_sensitive_chunk(
    settings, spans
):
    """Only internal chunks read, but the question has an email: withheld at internal."""
    question = "Did jane.roe@example.com approve the Beta deal?"
    with exported_logs() as exporter:
        _answered(settings, "internal", question=question)
    bodies = [body for body, _ in _content_records(exporter)]
    assert bodies == ["thread c1: content: withheld (pii)"] * 2
    exported = f"{_content_records(exporter)} {_content_events(spans)}"
    assert "jane.roe" not in exported

    settings.telemetry_content_max_sensitivity = "pii"
    with exported_logs() as exporter:
        _answered(settings, "internal", question=question)
    assert _content_records(exporter)[-1][1]["nexo.content.question"] == question


def test_reviewer_comment_and_router_reason_follow_the_gate(settings):
    """Free text #42 kept off the summary line is gated with the question and answer."""
    with exported_logs() as exporter:
        _answered(settings, "internal", comment="SSN 123-45-6789 on page 2")
    assert _content_records(exporter)[-1][0] == "thread c1: content: withheld (pii)"

    for reason, verdict in (
        ("not about the knowledge graph", "released (public)"),
        ("asks for jane.roe@example.com's address", "withheld (pii)"),
    ):
        refused = runtime(
            settings,
            FakeInvestigator([grounded_output()]),
            [route("rejected", reason=reason)],
        )
        with exported_logs() as exporter:
            refused.start(QUESTION, thread_id="r1")
        body, attrs = _content_records(exporter)[-1]
        assert body == f"thread r1: content: {verdict}"
        assert (attrs.get("nexo.content.routing_reason") == reason) is (
            verdict.startswith("released")
        )


@pytest.mark.parametrize(
    "detector, reason", [(BrokenDetector(), "detector failed"), (None, "no detector")]
)
def test_a_failing_or_missing_detector_withholds_content(
    settings, spans, detector, reason
):
    """Fail closed: a run that cannot be classified is never exported, even at phi."""
    settings.telemetry_content_max_sensitivity = "phi"
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.detector = detector
    with exported_logs() as exporter:
        rt.start(QUESTION, thread_id="d1")
    body, attrs = _content_records(exporter)[-1]
    assert body == f"thread d1: content: withheld ({reason})"
    assert not any(k.startswith("nexo.content.") for k in attrs)
    assert not _content_events(spans)


@pytest.mark.parametrize("capture", ["no_content", "span_only"])
def test_detector_none_withholds_unless_capture_forces_it(settings, capture):
    """SENSITIVE_DATA_PROVIDER=none cannot classify: an email and SSN are not logged."""
    settings.otel_instrumentation_genai_capture_message_content = capture
    question = "Is jane.roe@example.com (SSN 123-45-6789) on the Beta deal?"
    rt = runtime(settings, FakeInvestigator([grounded_output()]))
    rt.detector = NoopDetector()  # what build_detector("none") returns
    with exported_logs() as exporter:
        rt.start(question, thread_id="n1")
    body, attrs = _content_records(exporter)[-1]
    if capture == "span_only":
        assert body == "thread n1: content: released (content capture on)"
        assert attrs["nexo.content.question"] == question
    else:
        assert body == "thread n1: content: withheld (no detector)"
        assert "jane.roe" not in str(attrs)


def test_the_content_record_reaches_otlp_whatever_the_log_level(settings):
    """LOG_LEVEL=WARNING keeps the INFO audit record for OTLP and off stderr."""
    with exported_logs() as exporter:
        _answered(settings, "internal")
    assert [body for body, _ in _content_records(exporter)] == [
        "thread c1: content: released (internal)"
    ] * 2
    assert "content:" not in exporter.stderr.getvalue()


@pytest.mark.parametrize(
    "threshold, verdict", [("pii", "withheld (phi)"), ("phi", "released (phi)")]
)
def test_a_failed_agent_run_without_its_ledger_counts_as_phi(
    settings, threshold, verdict
):
    """A timeout or crash drops the chunks the agent read, so the run is assumed phi."""
    settings.telemetry_content_max_sensitivity = threshold
    rt = runtime(settings, FakeInvestigator([RuntimeError("agent crashed")]))
    with exported_logs() as exporter:
        rt.start(QUESTION, thread_id="f1")
    assert _content_records(exporter)[-1][0] == f"thread f1: content: {verdict}"


def test_content_capture_forces_export(settings):
    """The all-or-nothing capture switch exports content whatever the setting."""
    settings.telemetry_content_max_sensitivity = "none"
    settings.otel_instrumentation_genai_capture_message_content = "span_only"
    question = "Did jane.roe@example.com approve the Beta deal?"
    with exported_logs() as exporter:
        _answered(settings, "phi", question=question)
    body, attrs = _content_records(exporter)[-1]
    assert body == "thread c1: content: released (content capture on)"
    assert attrs["nexo.content.question"] == question


def test_an_unranked_level_counts_as_phi():
    """A level Nexo does not know is the most sensitive, not the least."""
    values = {"agent_outputs": [{"fetched_sensitivity": {CHUNK: "secret"}}]}
    assert run_sensitivity(values, RegexDetector(), []) == Sensitivity.PHI
    assert run_sensitivity({}, RegexDetector(), ["plain"]) == Sensitivity.PUBLIC
    settings = Settings(_env_file=None, telemetry_content_max_sensitivity="pii")
    values["user_query"] = QUESTION
    assert content_gate(settings, RegexDetector(), values) == ("withheld (phi)", {})


def test_stderr_lines_never_carry_the_content():
    """Only the OTLP handler exports the texts; a JSON stderr line has the verdict alone."""
    record = logging.LogRecord(
        "nexo.service", logging.INFO, __file__, 1, "content: released", None, None
    )
    record.content = {"nexo.content.question": QUESTION}
    assert QUESTION not in JsonFormatter().format(record)


def _with_content(workdir, value):
    return _setup(
        workdir,
        "mac-single",
        lambda d: d["spec"].setdefault("telemetry", {}).update(content=value),
    )


def test_setup_file_sets_the_threshold_and_the_default_is_rendered(workdir, tmp_path):
    """spec.telemetry.content reaches nexo.env, the default included, and beats a stale .env."""
    _, _, env = _render(workdir, _setup(workdir, "mac-single"))
    assert env["TELEMETRY_CONTENT_MAX_SENSITIVITY"] == "internal"
    stale = tmp_path / "stale.env"
    stale.write_text("TELEMETRY_CONTENT_MAX_SENSITIVITY=phi\n")
    for declared in THRESHOLDS:
        env = build_env(load_casting(_with_content(workdir, declared)))
        assert env["TELEMETRY_CONTENT_MAX_SENSITIVITY"] == declared
        rendered = tmp_path / f"{declared}.env"
        rendered.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        settings = Settings(_env_file=(stale, rendered))
        assert settings.telemetry_content_max_sensitivity == declared
    with pytest.raises(SetupError, match="content"):
        load_casting(_with_content(workdir, "secret"))


def test_langsmith_never_receives_the_content_event(workdir):
    """Self-hosted backends keep the nexo.content event; the LangSmith pipeline drops it."""
    _, collector, _ = _render(
        workdir, _setup(workdir, "mac-single"), addons={"langfuse", "langsmith"}
    )
    pipelines = collector["service"]["pipelines"]
    assert "filter/strip-nexo-content" in pipelines["traces/langsmith"]["processors"]
    assert "filter/strip-nexo-content" not in pipelines["traces"]["processors"]
    assert "filter/strip-nexo-content" not in pipelines["traces/langfuse"]["processors"]


THRESHOLD_ENV = "TELEMETRY_CONTENT_MAX_SENSITIVITY"
CAPTURE_ENV = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"


@pytest.mark.parametrize(
    "declared, dotenv, expected",
    [
        ("internal", "", "internal, detector regex: questions and answers of runs"),
        ("none", "", "none: no question or answer is logged"),
        ("internal", "SENSITIVE_DATA_PROVIDER=none", "detector none"),
        ("internal", f"{CAPTURE_ENV}=span_only", "=span_only logs every run"),
    ],
)
def test_check_shows_the_content_policy_in_force(
    workdir, tmp_path, declared, dotenv, expected
):
    """make check reads the threshold, detector and capture switch as Nexo will (.env too)."""
    casting = load_casting(_with_content(workdir, declared))
    rendered = tmp_path / "nexo.env"
    rendered.write_text("".join(f"{k}={v}\n" for k, v in build_env(casting).items()))
    (tmp_path / ".env").write_text(dotenv + "\n")
    finding = check.content_finding(
        casting, lambda: Settings(_env_file=(tmp_path / ".env", rendered))
    )
    assert finding.name == "content" and finding.ok
    assert expected in finding.detail
    if "detector none" in expected:
        assert "no question or answer is logged" in finding.detail


def test_check_names_an_environment_override(workdir, monkeypatch):
    """Only the process environment beats nexo.env's threshold; check says so."""
    monkeypatch.setenv(THRESHOLD_ENV, "pii")
    finding = check.content_finding(
        load_casting(_with_content(workdir, "internal")),
        lambda: Settings(_env_file=None),
    )
    assert "pii, detector regex" in finding.detail
    assert "environment overrides spec.telemetry.content internal" in finding.detail


def test_check_reports_an_invalid_capture_value_instead_of_echoing_it(
    workdir, monkeypatch
):
    """A value Settings rejects fails the finding with the variable's name."""
    monkeypatch.setenv(CAPTURE_ENV, "everything")
    finding = check.content_finding(
        load_casting(_setup(workdir, "mac-single")), lambda: Settings(_env_file=None)
    )
    assert not finding.ok
    assert finding.detail == f"cannot tell what is logged: {CAPTURE_ENV} invalid"
