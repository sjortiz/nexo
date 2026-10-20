"""Read-time redaction: a chunk above the caller's clearance may still be shown, masked.

A chunk is classified `pii` when a detector finds a personal detail in it. For a
caller below that level the rest of the passage is usually harmless, so it is
shown with the detected spans replaced by `[EMAIL]`, `[PHONE]` and so on. This
layer sits between a chunk store and the agent's tools and seeds, so it works
the same for any store. Everything it cannot mask span by span is withheld.
"""

from __future__ import annotations

from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.sensitive_data import (
    Detection,
    SensitiveDataDetector,
    classify,
    redact,
)
from nexo.storage.vector_store import ChunkFilter, ChunkStore, tokenize

# Search asks the store for this many candidates per result wanted, because
# chunks that cannot be shown still take a rank.
OVERFETCH = 3


class ChunkRedactor:
    """Decides, per caller, whether a chunk is shown as stored, masked or withheld."""

    def __init__(self, detector: SensitiveDataDetector) -> None:
        self._detector = detector

    def visible(
        self, record: ChunkRecord, chunk_filter: ChunkFilter
    ) -> ChunkRecord | None:
        """The chunk as this caller may see it, or None to withhold it.

        Redacted only when every reason the chunk is above the caller's clearance
        is a detected PII span: the document's declared level is within clearance,
        the detector finds exactly the level the chunk was stored at, and no
        detected span above clearance is PHI. The masked text is checked again
        and withheld if the detector still finds anything above clearance.
        """
        if chunk_filter.allows(record):
            return record
        if not self._widest(chunk_filter).allows(record):
            return None  # outside the router's scope, not only above clearance
        clearance = chunk_filter.max_sensitivity
        hidden = self._maskable(record, clearance)
        if not hidden:
            return None
        masked = redact(record.text, _merge(hidden))
        if _above(self._detector.detect(masked), clearance):
            return None
        return record.model_copy(update={"text": masked})

    def _maskable(self, record: ChunkRecord, clearance: Sensitivity) -> list[Detection]:
        """The spans to mask, or none when the chunk must be withheld whole."""
        floor = record.declared_sensitivity
        if floor is not None and floor.rank > clearance.rank:
            return []
        detections = self._detector.detect(record.text)
        # None means a chunk stored before the floor was recorded: assume the
        # detected spans are the whole reason (re-ingest to make this exact).
        if classify(detections, floor or Sensitivity.PUBLIC) != record.sensitivity:
            return []
        hidden = _above(detections, clearance)
        if any(d.sensitivity != Sensitivity.PII for d in hidden):
            return []
        return hidden

    def search(
        self, chunks: ChunkStore, query: str, k: int, chunk_filter: ChunkFilter
    ) -> list[ChunkRecord]:
        """Top-k chunks the caller may see, masked where needed.

        The store ranks the original text (its index holds it); masking happens
        after ranking. The query is masked first, so searching for a whole
        detected value such as an email address finds nothing. A masked chunk is
        returned only if the masked query matches its masked text (see
        `_matches_masked`), so fragments of a masked value (an area code, the
        local part of an address) do not find it either.
        """
        clearance = chunk_filter.max_sensitivity
        masked_query = redact(
            query, _merge(_above(self._detector.detect(query), clearance))
        )
        terms = set(tokenize(masked_query))
        found = chunks.search(masked_query, k * OVERFETCH, self._widest(chunk_filter))
        shown = []
        for record in found:
            view = self.visible(record, chunk_filter)
            if view is None:
                continue
            if view.text != record.text and not _matches_masked(
                terms, record.text, view.text
            ):
                continue
            shown.append(view)
        return shown[:k]

    @staticmethod
    def _widest(chunk_filter: ChunkFilter) -> ChunkFilter:
        """The same scope with no clearance limit."""
        return chunk_filter.model_copy(update={"max_sensitivity": Sensitivity.PHI})


def _above(detections: list[Detection], clearance: Sensitivity) -> list[Detection]:
    return [d for d in detections if d.sensitivity.rank > clearance.rank]


def _matches_masked(terms: set[str], original: str, masked: str) -> bool:
    """True if the query matches the masked text without help from masked-out words.

    Fails closed: at least one query term must be a word of the masked text (the
    placeholders' own words, such as "email" from [EMAIL], do not count), and no
    query term may be a word that masking removed. So a hit found only by its
    embedding, which was computed over the original text, is dropped, and so is
    one whose keyword match leans on a fragment of a masked value.
    """
    before = set(tokenize(original))
    kept = set(tokenize(masked)) & before
    return bool(terms & kept) and not terms & (before - kept)


def _merge(detections: list[Detection]) -> list[Detection]:
    """Join overlapping spans so each character is masked once."""
    merged: list[Detection] = []
    for d in sorted(detections, key=lambda d: (d.begin, -d.end)):
        if merged and d.begin < merged[-1].end:
            last = merged[-1]
            merged[-1] = Detection(
                last.category, last.begin, max(last.end, d.end), last.sensitivity
            )
        else:
            merged.append(d)
    return merged
