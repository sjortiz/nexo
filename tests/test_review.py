"""The shared review core: listing paused threads and reading their cited passages."""

from __future__ import annotations

import pytest

from nexo.agents.redaction import ChunkRedactor
from nexo.service import Runtime, open_checkpointer
from nexo.storage.schemas import Sensitivity
from nexo.storage.sensitive_data import RegexDetector
from tests.conftest import make_chunk
from tests.test_graph import CHUNK, FakeInvestigator, grounded_output, runtime

EMAIL = "jane.roe@beta.example"
APPROVE = {"approval_status": "approved", "reviewer": "a"}


def _paused(settings, saver, *thread_ids, chunks=None) -> Runtime:
    rt = runtime(settings, FakeInvestigator([grounded_output()]), checkpointer=saver)
    rt.chunks = chunks
    for thread_id in thread_ids:
        rt.start("q", thread_id=thread_id)
    return rt


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pending_reviews_lists_paused_threads_newest_first(settings, backend):
    """Threads awaiting review are listed newest first, with the packet status() shows."""
    settings.checkpoint_backend = backend
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", "t2", "t3")
        listed = rt.pending_reviews()
        assert [r.thread_id for r in listed] == ["t3", "t2", "t1"]
        assert all(r.status == "awaiting_review" for r in listed)
        assert listed[0] == rt.status("t3")
        assert "draft_answer" in (listed[0].pending_review or {})


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pending_reviews_skips_reviewed_and_unpaused_threads(settings, backend):
    """A reviewed thread, a router rejection and an empty store are never listed."""
    settings.checkpoint_backend = backend
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver)
        assert not rt.pending_reviews()
        rt = _paused(settings, saver, "t1", "t2")
        rt.review("t1", APPROVE)
        assert [r.thread_id for r in rt.pending_reviews()] == ["t2"]


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_pending_reviews_honours_the_limit(settings, backend):
    """The limit keeps the newest paused threads."""
    settings.checkpoint_backend = backend
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", "t2", "t3")
        assert [r.thread_id for r in rt.pending_reviews(limit=2)] == ["t3", "t2"]
        assert not rt.pending_reviews(limit=0)


def test_pending_reviews_loads_state_only_for_paused_threads(settings):
    """The scan reads each thread's newest checkpoint; only paused ones load full state."""
    settings.checkpoint_backend = "sqlite"
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", "t2")
        rt.review("t1", APPROVE)
        loaded = []
        original = rt._pending  # pylint: disable=protected-access
        rt._pending = (  # type: ignore  # pylint: disable=protected-access
            lambda tid: loaded.append(tid) or original(tid)
        )
        rt.pending_reviews()
    assert loaded == ["t2"]


def test_pending_reviews_sees_threads_from_another_process(settings):
    """A fresh checkpointer on the same SQLite file lists what an earlier one paused."""
    settings.checkpoint_backend = "sqlite"
    with open_checkpointer(settings) as saver:
        _paused(settings, saver, "t1")
    with open_checkpointer(settings) as saver:
        rt = runtime(
            settings, FakeInvestigator([grounded_output()]), checkpointer=saver
        )
        assert [r.thread_id for r in rt.pending_reviews()] == ["t1"]


def _store_with(chunk_store, record):
    object.__setattr__(record, "chunk_id", CHUNK)
    chunk_store.upsert([record], [record.text])
    return chunk_store


def _redacting(rt):
    rt.redactor = ChunkRedactor(RegexDetector())
    return rt


def test_review_passages_reads_cited_chunks_within_clearance(settings, chunk_store):
    """A cited chunk inside the reviewer's clearance is shown with its classification."""
    _store_with(chunk_store, make_chunk("doc", 0, "Acme bought Beta.", title="Deal"))
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", chunks=chunk_store)
        [passage] = rt.review_passages("t1", "internal")
    assert passage["status"] == "shown" and passage["text"] == "Acme bought Beta."
    assert passage["classification"] == "internal" and passage["source"] == "Deal"


def test_review_passages_withholds_chunks_above_clearance(settings, chunk_store):
    """A PHI chunk is withheld below phi clearance and shown at it."""
    _store_with(
        chunk_store,
        make_chunk("doc", 0, "Acme bought Beta.", Sensitivity.PHI),
    )
    with open_checkpointer(settings) as saver:
        rt = _redacting(_paused(settings, saver, "t1", chunks=chunk_store))
        [low] = rt.review_passages("t1", Sensitivity.INTERNAL)
        [high] = rt.review_passages("t1", "phi")
    assert low["status"] == "withheld" and low["text"] is None
    assert high["status"] == "shown" and high["text"] == "Acme bought Beta."


def test_review_passages_masks_pii_below_clearance(settings, chunk_store):
    """Below the chunk's clearance the reviewer sees masked text, never the raw email."""
    text = f"Contact Jane at {EMAIL} about the deal."
    _store_with(
        chunk_store,
        make_chunk(
            "doc", 0, text, Sensitivity.PII, declared_sensitivity=Sensitivity.INTERNAL
        ),
    )
    with open_checkpointer(settings) as saver:
        rt = _redacting(_paused(settings, saver, "t1", chunks=chunk_store))
        [masked] = rt.review_passages("t1", "internal")
        [whole] = rt.review_passages("t1", "pii")
    assert masked["status"] == "shown" and EMAIL not in masked["text"]
    assert "[EMAIL]" in masked["text"] and whole["text"] == text


def test_review_passages_without_detector_withholds_instead_of_leaking(
    settings, chunk_store
):
    """The default redactor has no detector, so a PII chunk is withheld, not shown raw."""
    _store_with(
        chunk_store,
        make_chunk(
            "doc",
            0,
            f"Mail {EMAIL}",
            Sensitivity.PII,
            declared_sensitivity=Sensitivity.INTERNAL,
        ),
    )
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", chunks=chunk_store)
        [passage] = rt.review_passages("t1", "internal")
    assert passage["status"] == "withheld" and passage["text"] is None


def test_review_passages_reports_missing_chunks_and_refuses_bad_threads(
    settings, chunk_store
):
    """A chunk missing from the store is not_found; unpaused threads and missing stores raise."""
    with open_checkpointer(settings) as saver:
        rt = _paused(settings, saver, "t1", chunks=chunk_store)
        assert rt.review_passages("t1", "phi")[0]["status"] == "not_found"
        with pytest.raises(ValueError, match="not awaiting review"):
            rt.review_passages("nope", "phi")
        rt.chunks = None
        with pytest.raises(ValueError, match="no chunk store"):
            rt.review_passages("t1", "phi")
        rt.chunks = chunk_store
        rt.review("t1", APPROVE)
        with pytest.raises(ValueError, match="not awaiting review"):
            rt.review_passages("t1", "phi")
