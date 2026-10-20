"""Hybrid retrieval: BM25 keyword ranking, vector similarity, and their fusion."""

from __future__ import annotations

import pytest

from nexo.llm import build_embeddings
from nexo.storage.schemas import Sensitivity
from nexo.storage.vector_store import (
    BM25,
    ChunkFilter,
    LocalChunkStore,
    fuse,
    tokenize,
)
from tests.conftest import make_chunk

TEXTS = (
    "Order XR-7731 shipped to Gamma Clinics on Monday.",
    "Beta Labs makes diagnostic kits for clinics.",
    "Acme Holdings compró Beta Labs en 2024.",
    "Quarterly revenue grew across every region.",
)


@pytest.fixture(name="store_factory")
def fixture_store_factory(settings, tmp_path):
    """Build a chunk store in the given retrieval mode, seeded with TEXTS as one document."""

    def build(mode: str, texts=TEXTS, sensitivity=Sensitivity.INTERNAL):
        store = LocalChunkStore(
            build_embeddings(settings), tmp_path / f"{mode}.json", mode=mode
        )
        chunks = [
            make_chunk("doc", i, text, sensitivity) for i, text in enumerate(texts)
        ]
        store.upsert(chunks, [c.text for c in chunks])
        return store, chunks

    return build


def test_tokenize_folds_case_and_accents():
    """Tokens are lowercased, accent-stripped and split on punctuation."""
    assert tokenize("Acme COMPRÓ Beta-Labs, año 2024") == [
        "acme",
        "compro",
        "beta",
        "labs",
        "ano",
        "2024",
    ]


def test_bm25_prefers_rare_terms_and_ignores_unknown_ones():
    """BM25 ranks by query-term matches and scores nothing for unknown terms."""
    index = BM25(
        {
            "a": tokenize("clinics clinics kits"),
            "b": tokenize("clinics revenue"),
            "c": tokenize("revenue"),
        }
    )
    scores = index.scores("kits clinics")
    assert max(scores, key=scores.get) == "a"
    assert "c" not in scores  # no query term: no score at all
    assert not index.scores("nonexistent words")


def test_bm25_idf_lets_a_rare_term_beat_a_repeated_common_one():
    """One occurrence of a rare term outranks three of a term found in most documents."""
    index = BM25(
        {
            "common": tokenize("clinics clinics clinics"),
            "rare": tokenize("kits"),
            "c": tokenize("clinics"),
            "d": tokenize("clinics"),
        }
    )
    scores = index.scores("clinics kits")
    assert scores["rare"] > scores["common"]


def test_keyword_mode_finds_exact_codes_and_accent_variants(store_factory):
    """Keyword search matches exact codes and unaccented spellings, without random fill."""
    store, chunks = store_factory("keyword")
    assert [c.chunk_id for c in store.search("XR-7731", 3, ChunkFilter())] == [
        chunks[0].chunk_id
    ]
    assert store.search("compro", 3, ChunkFilter())[0].chunk_id == chunks[2].chunk_id
    assert (
        store.search("completely unrelated phrase", 3, ChunkFilter()) == []
    )  # no random fill


def test_hybrid_puts_the_exact_match_first_and_still_fills_with_vectors(store_factory):
    """Hybrid search ranks the keyword match first and fills the rest from vectors."""
    store, chunks = store_factory("hybrid")
    results = store.search("XR-7731", 3, ChunkFilter())
    assert results[0].chunk_id == chunks[0].chunk_id
    assert len(results) == 3  # vector similarity contributes the rest


def test_vector_mode_has_no_keyword_component(store_factory):
    """Vector search returns results even for text sharing no keywords with the corpus."""
    store, _ = store_factory("vector")
    assert (
        len(store.search("completely unrelated phrase", 2, ChunkFilter())) == 2
    )  # vectors always rank


@pytest.mark.parametrize("mode", ["keyword", "hybrid", "vector"])
def test_every_mode_applies_the_filter(store_factory, mode):
    """All retrieval modes exclude chunks above the filter's sensitivity ceiling."""
    store, _ = store_factory(mode, sensitivity=Sensitivity.PHI)
    assert (
        store.search("XR-7731", 3, ChunkFilter(max_sensitivity=Sensitivity.INTERNAL))
        == []
    )
    assert store.search("XR-7731", 3, ChunkFilter(max_sensitivity=Sensitivity.PHI))


def test_keyword_index_follows_writes_and_persistence(
    store_factory, settings, tmp_path
):
    """The keyword index reflects deletes and upserts and survives reopening the store."""
    store, chunks = store_factory("keyword")
    assert store.search("XR-7731", 1, ChunkFilter())  # builds the index
    store.delete_source("doc", keep={c.chunk_id for c in chunks[1:]})
    assert (
        store.search("XR-7731", 1, ChunkFilter()) == []
    )  # deleted chunk no longer found
    new = make_chunk("doc2", 0, "Invoice ZQ-9 settled.")
    store.upsert([new], [new.text])
    assert [c.chunk_id for c in store.search("ZQ-9", 1, ChunkFilter())] == [
        new.chunk_id
    ]
    reopened = LocalChunkStore(
        build_embeddings(settings), tmp_path / "keyword.json", mode="keyword"
    )
    assert [c.chunk_id for c in reopened.search("ZQ-9", 1, ChunkFilter())] == [
        new.chunk_id
    ]


def test_rrf_rewards_agreement_between_rankings():
    """Reciprocal rank fusion favours items present in several rankings."""
    assert (
        fuse([["a", "b", "c"], ["c", "x"]])[0] == "c"
    )  # in both lists beats first in one
    assert fuse([["a", "b"]]) == ["a", "b"]


def test_unknown_mode_is_rejected(settings):
    """An unsupported retrieval mode raises ValueError."""
    with pytest.raises(ValueError):
        LocalChunkStore(build_embeddings(settings), mode="semantic")
