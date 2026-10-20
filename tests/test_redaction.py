"""Read-time redaction: a chunk that is sensitive only because of detected personal
details is shown masked to a lower clearance and whole to a cleared caller; anything
else above clearance stays withheld."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from strands.models.openai import OpenAIModel

from nexo.agents.investigator import Investigator
from nexo.cli import _judge_evidence
from nexo.evaluation import judge_faithfulness
from nexo.agents.models import AgentOutput, InvestigationScope
from nexo.agents.redaction import ChunkRedactor
from nexo.agents.tools import InvestigationContext, build_investigation_tools
from nexo.orchestration.compliance import run_compliance_checks
from nexo.storage.neo4j_chunk_store import _record
from nexo.storage.schemas import Sensitivity
from nexo.storage.sensitive_data import Detection, NoopDetector, RegexDetector
from nexo.llm import build_embeddings
from nexo.storage.vector_store import ChunkFilter, LocalChunkStore
from tests.conftest import FakeGraph, make_chunk
from tests.test_agent import ScriptedClient, _completion, _tool_call

EMAIL = "jane.roe@beta.example"
LEADERSHIP = (
    f"Jane Roe is chief executive of Beta Labs and led product at Delta Devices. "
    f"Investor relations contact: {EMAIL}"
)
DETECTOR = RegexDetector()


def _leadership(floor: Sensitivity | None = Sensitivity.INTERNAL, text=LEADERSHIP):
    return make_chunk("note", 0, text, Sensitivity.PII, declared_sensitivity=floor)


def _tools(settings, chunk_store, clearance, detector=DETECTOR, budget=10):
    ctx = InvestigationContext(
        request_id="r1",
        max_tool_calls=budget,
        scope=InvestigationScope(chunk_filter=ChunkFilter(max_sensitivity=clearance)),
    )
    tools = {
        t.tool_name: t
        for t in build_investigation_tools(
            graph=FakeGraph(),
            chunks=chunk_store,
            ctx=ctx,
            settings=settings,
            redactor=ChunkRedactor(detector),
        )
    }
    return ctx, tools["fetch_vector_chunks"], tools["search_chunks"]


def _store(chunk_store, *chunks):
    chunk_store.upsert(list(chunks), [c.text for c in chunks])


def test_internal_caller_gets_the_chunk_with_the_email_masked(settings, chunk_store):
    """Fetch and search both show the same chunk id with [EMAIL]; the ledger keeps its level."""
    chunk = _leadership()
    _store(chunk_store, chunk)
    ctx, fetch, search = _tools(settings, chunk_store, Sensitivity.INTERNAL)

    fetched = json.loads(fetch([chunk.chunk_id]))
    found = json.loads(search("Delta Devices"))

    for out in (fetched, found):
        [view] = out["chunks"]
        assert view["chunk_id"] == chunk.chunk_id
        assert view["text"].endswith("Investor relations contact: [EMAIL]")
        assert "Delta Devices" in view["text"] and "@" not in view["text"]
    assert fetched["withheld"] == []
    assert ctx.evidence.fetched == {chunk.chunk_id: "pii"}


def test_cleared_caller_gets_the_original_text(settings, chunk_store):
    """At or above the chunk's clearance nothing is masked."""
    chunk = _leadership()
    _store(chunk_store, chunk)
    for clearance in (Sensitivity.PII, Sensitivity.PHI):
        _, fetch, search = _tools(settings, chunk_store, clearance)
        assert json.loads(fetch([chunk.chunk_id]))["chunks"][0]["text"] == LEADERSHIP
        assert json.loads(search("Delta"))["chunks"][0]["text"] == LEADERSHIP


@pytest.mark.parametrize(
    ("chunk", "why"),
    [
        (_leadership(floor=Sensitivity.PII), "document declared pii"),
        (
            make_chunk(
                "note",
                1,
                "Jane Roe led product at Delta Devices.",
                Sensitivity.PII,
                declared_sensitivity=Sensitivity.PII,
            ),
            "pii with no detected span",
        ),
        (
            make_chunk(
                "note",
                2,
                f"Delta Devices, MRN: A1234567, {EMAIL}",
                Sensitivity.PHI,
                declared_sensitivity=Sensitivity.INTERNAL,
            ),
            "PHI span cannot be masked by policy",
        ),
        (
            make_chunk(
                "note",
                3,
                f"Delta Devices {EMAIL}",
                Sensitivity.PHI,
                declared_sensitivity=Sensitivity.INTERNAL,
            ),
            "stored higher than the detector finds",
        ),
    ],
)
def test_classification_not_from_maskable_spans_stays_withheld(
    settings, chunk_store, chunk, why
):
    """Fetch withholds, search hides it, and neither records it as evidence."""
    _store(chunk_store, chunk)
    ctx, fetch, search = _tools(settings, chunk_store, Sensitivity.INTERNAL)

    out = json.loads(fetch([chunk.chunk_id]))
    assert out["chunks"] == [] and out["withheld"] == [chunk.chunk_id], why
    assert json.loads(search("Delta Devices"))["chunks"] == [], why
    assert not ctx.evidence.fetched


def test_redaction_needs_a_detector_and_the_router_scope(settings, chunk_store):
    """No detector: whole-chunk withholding as before. A scope miss is never redacted."""
    chunk = _leadership()
    _store(chunk_store, chunk)
    _, fetch, _ = _tools(
        settings, chunk_store, Sensitivity.INTERNAL, detector=NoopDetector()
    )
    assert json.loads(fetch([chunk.chunk_id]))["withheld"] == [chunk.chunk_id]

    scoped = ChunkFilter(max_sensitivity=Sensitivity.INTERNAL, doc_types=["contract"])
    assert ChunkRedactor(DETECTOR).visible(chunk, scoped) is None  # doc_type is general


def test_chunks_stored_before_the_floor_was_recorded_are_still_masked(
    settings, chunk_store
):
    """declared_sensitivity=None (older store): the detected spans are taken as the reason."""
    chunk = _leadership(floor=None)
    _store(chunk_store, chunk)
    _, fetch, _ = _tools(settings, chunk_store, Sensitivity.INTERNAL)
    assert "[EMAIL]" in json.loads(fetch([chunk.chunk_id]))["chunks"][0]["text"]


@pytest.mark.parametrize(
    "secret",
    [EMAIL, "415-555-0134", "+1 415 555 0134", "4111 1111 1111 1111"],
)
def test_masked_text_has_no_detectable_span(secret):
    """Whatever kind of span, the masked text leaves nothing for the detector to find."""
    chunk = make_chunk(
        "n",
        0,
        f"Delta Devices contact {secret} for details.",
        Sensitivity.PII,
        declared_sensitivity=Sensitivity.INTERNAL,
    )
    shown = ChunkRedactor(DETECTOR).visible(
        chunk, ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
    )
    assert shown is not None and secret not in shown.text
    assert not DETECTOR.detect(shown.text)
    assert shown.chunk_id == chunk.chunk_id


class _Overlapping:  # pylint: disable=too-few-public-methods
    """Two detections over the same digits, as a phone and a card pattern can produce."""

    def detect(self, text):
        """Overlapping spans around the digits, nothing in already-masked text."""
        start = text.find("41555501349")
        if start < 0:
            return []
        return [
            Detection("PHONE", start, start + 10, Sensitivity.PII),
            Detection("CARD", start + 3, start + 11, Sensitivity.PII),
        ]


def test_overlapping_detections_are_masked_once_without_leaving_pieces():
    """Naive span-by-span replacement of overlapping spans would leave digits behind."""
    chunk = make_chunk(
        "n",
        0,
        "Call 41555501349 today",
        Sensitivity.PII,
        declared_sensitivity=Sensitivity.INTERNAL,
    )
    scope = ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
    shown = ChunkRedactor(_Overlapping()).visible(chunk, scope)
    assert shown is not None and shown.text == "Call [PHONE] today"


class _SeesTheMask:  # pylint: disable=too-few-public-methods
    """A detector whose second pass flags the mask itself."""

    def detect(self, text):
        """Real findings, plus the placeholder when present."""
        found = DETECTOR.detect(text)
        if "[EMAIL]" in text:
            found.append(
                Detection("X", text.index("[EMAIL]"), len(text), Sensitivity.PII)
            )
        return found


def test_text_that_still_triggers_the_detector_after_masking_is_withheld():
    """Fail closed when the masked text still has something above clearance."""
    chunk = _leadership()
    scope = ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
    assert ChunkRedactor(DETECTOR).visible(chunk, scope) is not None
    assert ChunkRedactor(_SeesTheMask()).visible(chunk, scope) is None


def test_search_masks_the_query_and_looks_past_withheld_chunks(settings, tmp_path):
    """Searching for the email finds nothing for an internal caller (no probing), and a
    withheld top hit does not push the visible result out of a k=1 search."""
    chunk_store = LocalChunkStore(
        build_embeddings(settings), tmp_path / "keyword.json", mode="keyword"
    )
    phi = make_chunk(
        "p", 0, "Delta Devices Delta Devices Delta Devices", Sensitivity.PHI
    )
    public = make_chunk("p", 1, "Delta Devices makes sensors.")
    chunk = _leadership()
    _store(chunk_store, phi, public, chunk)
    _, _, internal = _tools(settings, chunk_store, Sensitivity.INTERNAL)
    _, _, cleared = _tools(settings, chunk_store, Sensitivity.PII)

    assert json.loads(internal(EMAIL))["chunks"] == []
    assert [c["chunk_id"] for c in json.loads(cleared(EMAIL))["chunks"]] == [
        chunk.chunk_id
    ]
    top = json.loads(internal("Delta Devices", 1))["chunks"]
    assert [c["chunk_id"] for c in top] and top[0]["chunk_id"] != phi.chunk_id


def test_masked_chunks_keep_the_email_out_of_spans_and_logs(
    settings, chunk_store, caplog
):
    """Tool spans carry counts only; nothing the tools emit contains the address."""
    chunk = _leadership()
    _store(chunk_store, chunk)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    _, fetch, search = _tools(settings, chunk_store, Sensitivity.INTERNAL)
    with caplog.at_level("DEBUG"):
        for call, arg in ((fetch, [chunk.chunk_id]), (search, "Delta Devices")):
            with provider.get_tracer("t").start_as_current_span("execute_tool"):
                call(arg)

    [fetch_span, search_span] = exporter.get_finished_spans()
    assert fetch_span.attributes["nexo.chunks.redacted"] == 1
    emitted = (
        json.dumps(
            [dict(s.attributes) for s in (fetch_span, search_span)]
            + [e.attributes for s in (fetch_span, search_span) for e in s.events]
        )
        + caplog.text
    )
    assert EMAIL not in emitted and "beta.example" not in emitted


def _output(answer, chunk):
    return AgentOutput(
        answer=answer,
        citations=[chunk.chunk_id],
        fetched_sensitivity={chunk.chunk_id: "pii"},
    )


def test_compliance_passes_the_masked_form_and_catches_the_real_address():
    """A draft that quotes the masked passage is clean; one that restores the email is blocked."""
    chunk = _leadership()
    masked = _output(f"Contact: [EMAIL] [chunk:{chunk.chunk_id}]", chunk)
    leaked = _output(f"Contact: {EMAIL} [chunk:{chunk.chunk_id}]", chunk)

    codes = {f.code for f in run_compliance_checks(masked, DETECTOR).findings}
    assert "sensitive_data_in_answer" not in codes
    leaked_codes = {f.code for f in run_compliance_checks(leaked, DETECTOR).findings}
    assert "sensitive_data_in_answer" in leaked_codes


def _seeded_request(settings, chunk_store, detector):
    """Run the investigator over a store holding one maskable chunk; return the first prompt."""
    _store(chunk_store, _leadership())
    client = ScriptedClient([_completion({"content": "No evidence."}, "stop")] * 2)
    investigator = Investigator(
        chunks=chunk_store,
        settings=settings,
        graph=FakeGraph(),
        model=OpenAIModel(client=client, model_id="m", stream=False),
    )
    if detector:
        investigator.with_detector(detector)
    investigator.investigate(
        "Who led product at Delta Devices?", scope=InvestigationScope()
    )
    return json.dumps(client.requests[0]["messages"])


def test_seed_passages_are_masked_for_the_model(settings, chunk_store):
    """The first prompt carries the masked passage; without a detector it carries none."""
    sent = _seeded_request(settings, chunk_store, DETECTOR)
    assert "Investor relations contact: [EMAIL]" in sent and EMAIL not in sent

    unmasked = _seeded_request(settings, chunk_store, None)
    assert "Investor relations" not in unmasked and EMAIL not in unmasked


def test_agent_reads_the_masked_chunk_through_its_tools(settings, chunk_store):
    """Graph strategy, fetch then answer: the tool result is masked and the citation holds."""
    chunk = _leadership()
    _store(chunk_store, chunk)
    client = ScriptedClient(
        [
            _tool_call("t1", "fetch_vector_chunks", {"chunk_ids": [chunk.chunk_id]}),
            _completion(
                {"content": f"Delta Devices [chunk:{chunk.chunk_id}]."}, "stop"
            ),
        ]
    )
    investigator = Investigator(
        chunks=chunk_store,
        settings=settings,
        graph=FakeGraph(),
        model=OpenAIModel(client=client, model_id="m", stream=False),
    ).with_detector(DETECTOR)

    output = investigator.investigate(
        "Who led product at Delta Devices?", scope=InvestigationScope(strategy="graph")
    )

    sent = json.dumps(client.requests[1]["messages"])
    assert "Investor relations contact: [EMAIL]" in sent and EMAIL not in sent
    assert output.citations == [chunk.chunk_id] and output.withheld_chunk_ids == []


def test_neo4j_chunk_node_keeps_the_declared_level():
    """Reading a (:Chunk) back must not drop the floor, or every chunk would look legacy."""
    chunk = _leadership(floor=Sensitivity.PII)
    props = chunk.model_dump(mode="json")
    assert _record(props).declared_sensitivity is Sensitivity.PII
    del props["declared_sensitivity"]  # a node written before the field existed
    assert _record(props).declared_sensitivity is None


class _RecordingJudge:
    """Stands in for the judge model: scores 5 and keeps every message it was sent."""

    def __init__(self):
        self.seen: list[str] = []

    def bind(self, **_kwargs):
        """Guided decoding is irrelevant here."""
        return self

    def invoke(self, messages, config=None):  # pylint: disable=unused-argument
        """Record the request and answer with a valid score."""
        self.seen.extend(str(m.content) for m in messages)
        return AIMessage(content='{"score": 5, "rationale": "ok"}')


def test_eval_judge_sees_the_masked_chunk_not_the_original(chunk_store):
    """An internal case with a pii-only chunk: the judge request has [EMAIL], never the address."""
    chunk = _leadership()
    other = make_chunk("note", 9, f"Secret MRN: A1234567 {EMAIL}", Sensitivity.PHI)
    _store(chunk_store, chunk, other)
    scope = ChunkFilter(max_sensitivity=Sensitivity.INTERNAL)
    judge = _RecordingJudge()

    evidence = _judge_evidence(
        chunk_store, ChunkRedactor(DETECTOR), scope, [chunk.chunk_id, other.chunk_id]
    )
    judge_faithfulness(judge, "Jane Roe led product at Delta Devices.", evidence)

    sent = "\n".join(judge.seen)
    assert "Investor relations contact: [EMAIL]" in sent
    assert EMAIL not in sent and "A1234567" not in sent  # withheld chunk is dropped
    assert len(evidence) == 1
