"""Vector chunk store, cross-referenced from Neo4j by `chunk_id`.

`ChunkStore` is the contract the rest of the stack depends on: exact
get-by-id (the graph -> text hop), filtered search (seeding), and
delete-by-document (re-ingestion). `LocalChunkStore` wraps LangChain's
`InMemoryVectorStore` with atomic JSON persistence for single-node use.

Search is hybrid by default: BM25 keyword ranking (exact names, codes, IDs
that embeddings blur) and vector similarity (paraphrases), fused with
Reciprocal Rank Fusion. Both run in-process with no extra dependency.

For production, the AWS vector-database guidance points to Aurora/RDS
PostgreSQL + pgvector for this access pattern (primary-key lookup, SQL
metadata filters, ACID writes so a chunk is readable as soon as Neo4j
references it), or OpenSearch Service when hybrid lexical+vector search or
very high query rates matter. Implement `ChunkStore` against either.
"""

from __future__ import annotations

import math
import os
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from pydantic import BaseModel, ConfigDict

from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.telemetry import traced_span

DATA_SOURCE_ID = "nexo-chunks"
RetrievalMode = Literal["hybrid", "vector", "keyword"]
RRF_K = 60  # standard Reciprocal Rank Fusion constant
_WORD = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip accents ("compró" == "compro"), split on word characters."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return _WORD.findall(folded)


class BM25:
    """Okapi BM25 over a fixed corpus (rebuilt when the corpus changes)."""

    def __init__(self, documents: dict[str, list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self._k1, self._b = k1, b
        self._tf = {doc_id: Counter(tokens) for doc_id, tokens in documents.items()}
        self._length = {doc_id: len(tokens) for doc_id, tokens in documents.items()}
        self._avg_length = (sum(self._length.values()) / len(self._length)) if self._length else 0.0
        df = Counter(term for tf in self._tf.values() for term in tf)
        n = len(self._tf)
        self._idf = {term: math.log(1 + (n - count + 0.5) / (count + 0.5)) for term, count in df.items()}

    def scores(self, query: str) -> dict[str, float]:
        terms = [t for t in tokenize(query) if t in self._idf]
        result: dict[str, float] = {}
        for doc_id, tf in self._tf.items():
            norm = self._k1 * (1 - self._b + self._b * self._length[doc_id] / (self._avg_length or 1))
            score = sum(self._idf[t] * tf[t] * (self._k1 + 1) / (tf[t] + norm) for t in terms if t in tf)
            if score > 0:
                result[doc_id] = score
        return result


class ChunkFilter(BaseModel):
    """Metadata pre-filter applied before similarity ranking and on fetch."""

    model_config = ConfigDict(extra="forbid")

    doc_types: list[str] | None = None
    source_ids: list[str] | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    max_sensitivity: Sensitivity = Sensitivity.INTERNAL

    def allows(self, chunk: ChunkRecord) -> bool:
        if chunk.sensitivity.rank > self.max_sensitivity.rank:
            return False
        if self.doc_types and chunk.doc_type not in self.doc_types:
            return False
        if self.source_ids and chunk.source_id not in self.source_ids:
            return False
        if self.effective_from or self.effective_to:
            if chunk.effective_date is None:
                return False
            if self.effective_from and chunk.effective_date < self.effective_from:
                return False
            if self.effective_to and chunk.effective_date > self.effective_to:
                return False
        return True


class ChunkStore(Protocol):
    def upsert(self, chunks: Sequence[ChunkRecord], embedding_texts: Sequence[str]) -> None: ...
    def get_by_ids(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]: ...
    def search(self, query: str, k: int, chunk_filter: ChunkFilter) -> list[ChunkRecord]: ...
    def delete_source(self, source_id: str, keep: set[str] | frozenset[str] = frozenset()) -> int: ...


class LocalChunkStore:
    """In-process vector index persisted to a JSON file (atomic replace)."""

    def __init__(self, embeddings: Embeddings, path: Path | None = None, mode: RetrievalMode = "hybrid") -> None:
        if mode not in ("hybrid", "vector", "keyword"):
            raise ValueError(f"unknown retrieval mode {mode!r}")
        self._embeddings = embeddings
        self._path = path
        self._mode = mode
        self._bm25: BM25 | None = None  # built lazily, dropped on every write
        self._lock = threading.RLock()
        if path is not None and path.exists():
            self._store = InMemoryVectorStore.load(str(path), embeddings)
        else:
            self._store = InMemoryVectorStore(embeddings)

    def upsert(self, chunks: Sequence[ChunkRecord], embedding_texts: Sequence[str]) -> None:
        if len(chunks) != len(embedding_texts):
            raise ValueError("chunks and embedding_texts must align")
        if not chunks:
            return
        documents = [
            # The embedded text carries headers/glossary; the record keeps the
            # exact chunk text for evidence checks and display.
            Document(id=c.chunk_id, page_content=t, metadata={"record": c.model_dump(mode="json")})
            for c, t in zip(chunks, embedding_texts, strict=True)
        ]
        with self._lock:
            self._store.add_documents(documents, ids=[c.chunk_id for c in chunks])
            self._bm25 = None
            self._persist()

    def get_by_ids(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]:
        with traced_span("chunk_store get_by_ids", {"nexo.chunks.requested": len(chunk_ids)}) as span, self._lock:
            documents = self._store.get_by_ids(list(chunk_ids))
            span.set_attribute("nexo.chunks.found", len(documents))
        by_id = {d.id: ChunkRecord.model_validate(d.metadata["record"]) for d in documents}
        return [by_id[i] for i in chunk_ids if i in by_id]

    def search(self, query: str, k: int, chunk_filter: ChunkFilter) -> list[ChunkRecord]:
        """Top-k chunks allowed by the filter, ranked by the store's retrieval mode."""

        def _allowed(document: Document) -> bool:
            return chunk_filter.allows(ChunkRecord.model_validate(document.metadata["record"]))

        # GenAI `retrieval` operation; INTERNAL because this store is in-process
        # (a pgvector/OpenSearch backend would be a CLIENT span).
        attributes = {"gen_ai.operation.name": "retrieval", "gen_ai.data_source.id": DATA_SOURCE_ID,
                      "gen_ai.retrieval.top_k": k, "nexo.retrieval.mode": self._mode}
        with traced_span(f"retrieval {DATA_SOURCE_ID}", attributes) as span, self._lock:
            candidates = k if self._mode == "vector" else max(k * 4, 20)
            rankings: list[list[str]] = []
            if self._mode in ("vector", "hybrid"):
                rankings.append([d.id for d in self._store.similarity_search(query, k=candidates, filter=_allowed)])
            if self._mode in ("keyword", "hybrid"):
                rankings.append(self._keyword_ranking(query, candidates, chunk_filter))
            ranked = _fuse(rankings)[:k]
            records = {d.id: d.metadata["record"] for d in self._store.get_by_ids(ranked)}
            span.set_attribute("nexo.chunks.found", len(ranked))
        return [ChunkRecord.model_validate(records[chunk_id]) for chunk_id in ranked]

    def _keyword_ranking(self, query: str, limit: int, chunk_filter: ChunkFilter) -> list[str]:
        if self._bm25 is None:
            self._bm25 = BM25({doc_id: tokenize(entry["text"]) for doc_id, entry in self._store.store.items()})
        scores = self._bm25.scores(query)
        allowed = [
            doc_id for doc_id in sorted(scores, key=lambda d: (-scores[d], d))
            if chunk_filter.allows(ChunkRecord.model_validate(self._store.store[doc_id]["metadata"]["record"]))
        ]
        return allowed[:limit]

    def delete_source(self, source_id: str, keep: set[str] | frozenset[str] = frozenset()) -> int:
        """Delete a document's chunks except `keep` (the current version's ids)."""
        with self._lock:
            stale = [
                doc_id
                for doc_id, entry in self._store.store.items()
                if entry["metadata"]["record"]["source_id"] == source_id and doc_id not in keep
            ]
            if stale:
                self._store.delete(stale)
                self._bm25 = None
                self._persist()
        return len(stale)

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        self._store.dump(str(tmp))
        os.replace(tmp, self._path)  # readers never see a half-written index


def _fuse(rankings: list[list[str]]) -> list[str]:
    """Reciprocal Rank Fusion; a single ranking passes through unchanged."""
    if len(rankings) == 1:
        return rankings[0]
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (RRF_K + rank)
    return sorted(scores, key=lambda d: (-scores[d], d))
