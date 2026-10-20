"""Chunk text and embeddings on the graph's own (:Chunk) nodes (ADR-0018).

The graph and its chunks live in one database, so they cannot drift apart:
every checkout of a stack sees both, and deleting a document removes both.
Search is hybrid like the local store: a vector index (cosine) and a full-text
index (Lucene, accents folded) fused with Reciprocal Rank Fusion. Candidates are
filtered with the same `ChunkFilter.allows` as the local store, so clearance rules
live in one place.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from langchain_core.embeddings import Embeddings
from neo4j import ManagedTransaction
from opentelemetry.trace import SpanKind

from nexo.storage.graph_store import GraphStore
from nexo.storage.schemas import ChunkRecord
from nexo.storage.vector_store import (
    DATA_SOURCE_ID,
    ChunkFilter,
    RetrievalMode,
    fuse,
    retrieval_attributes,
    tokenize,
)
from nexo.telemetry import traced_span

VECTOR_INDEX = "chunk_embedding"
TEXT_INDEX = "chunk_search_text"
# Hits are filtered after the index lookup: ask for more than k, then filter.
OVERFETCH = 10

# Node properties for a hit, without the embedding and search text (large, unused).
_HIT = (
    " YIELD node AS c, score WHERE c.text IS NOT NULL"
    " RETURN c {.*, embedding: null, search_text: null} AS c ORDER BY score DESC"
)
_RECORD_FIELDS = (
    "chunk_id",
    "source_id",
    "ordinal",
    "text",
    "section_path",
    "title",
    "doc_type",
    "source_uri",
    "effective_date",
    "sensitivity",
    "ingested_at",
    "schema_version",
)


def schema_statements(dimensions: int) -> tuple[str, ...]:
    """The vector and full-text indexes over (:Chunk); dimensions must match the model."""
    if not isinstance(dimensions, int) or dimensions <= 0:
        raise ValueError(
            f"embedding dimensions must be a positive int, not {dimensions!r}"
        )
    return (
        f"CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS FOR (c:Chunk) ON c.embedding "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimensions}, "
        "`vector.similarity_function`: 'cosine'}}",
        f"CREATE FULLTEXT INDEX {TEXT_INDEX} IF NOT EXISTS FOR (c:Chunk) ON EACH [c.search_text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-folding'}}",
    )


def _row(
    chunk: ChunkRecord, search_text: str, vector: Sequence[float]
) -> dict[str, Any]:
    props = chunk.model_dump(mode="json")
    props["search_text"] = search_text
    props["embedding"] = [float(v) for v in vector]
    return {"chunk_id": chunk.chunk_id, "props": props}


def _record(node: dict[str, Any]) -> ChunkRecord:
    values = {
        field: node.get(field)
        for field in _RECORD_FIELDS
        if node.get(field) is not None
    }
    if isinstance(values.get("effective_date"), str):
        values["effective_date"] = date.fromisoformat(values["effective_date"])
    if isinstance(values.get("ingested_at"), str):
        values["ingested_at"] = datetime.fromisoformat(values["ingested_at"])
    return ChunkRecord.model_validate(values)


def _hits(
    tx: ManagedTransaction, statement: str, **params: Any
) -> list[dict[str, Any]]:
    return [dict(r["c"]) for r in tx.run(statement, **params)]


def _visible(
    rows: list[dict[str, Any]],
    chunk_filter: ChunkFilter,
    limit: int,
    into: dict[str, ChunkRecord],
) -> list[str]:
    """Ids of the hits the caller may see (ChunkFilter.allows), best first, at most `limit`."""
    ids: list[str] = []
    for node in rows:
        record = _record(node)
        if chunk_filter.allows(record):
            into[record.chunk_id] = record
            ids.append(record.chunk_id)
        if len(ids) == limit:
            break
    return ids


class Neo4jChunkStore:
    """`ChunkStore` on the graph's (:Chunk) nodes, through the graph store's driver."""

    def __init__(
        self,
        graph: GraphStore,
        embeddings: Embeddings,
        *,
        dimensions: int,
        mode: RetrievalMode = "hybrid",
    ) -> None:
        if mode not in ("hybrid", "vector", "keyword"):
            raise ValueError(f"unknown retrieval mode {mode!r}")
        self._graph = graph
        self._embeddings = embeddings
        self._dimensions = dimensions
        self._mode = mode

    def ensure_schema(self) -> None:
        """Create the vector and full-text indexes, if missing (needs a writer account)."""
        with self._graph.session(write=True) as session:
            for statement in schema_statements(self._dimensions):
                session.run(statement).consume()
            # New indexes populate in the background; queries need them online.
            session.run("CALL db.awaitIndexes(120)").consume()

    # --- writes -----------------------------------------------------------------
    def upsert(
        self, chunks: Sequence[ChunkRecord], embedding_texts: Sequence[str]
    ) -> None:
        """Embed the aligned texts and store text, embedding and metadata on each chunk."""
        if len(chunks) != len(embedding_texts):
            raise ValueError("chunks and embedding_texts must align")
        if not chunks:
            return
        vectors = self._embeddings.embed_documents(list(embedding_texts))
        self.write(chunks, embedding_texts, vectors)

    def write(
        self,
        chunks: Sequence[ChunkRecord],
        search_texts: Sequence[str],
        vectors: Sequence[Sequence[float]],
    ) -> None:
        """Store already-embedded chunks (ingestion, or an import from the local store)."""
        if not len(chunks) == len(search_texts) == len(vectors):
            raise ValueError("chunks, search_texts and vectors must align")
        wrong = [len(v) for v in vectors if len(v) != self._dimensions]
        if wrong:
            raise ValueError(
                f"embeddings have {wrong[0]} dimensions; the index expects {self._dimensions}"
            )
        rows = [
            _row(c, t, v) for c, t, v in zip(chunks, search_texts, vectors, strict=True)
        ]
        with (
            traced_span(
                "chunk_store upsert",
                {"db.system.name": "neo4j", "nexo.chunks.written": len(rows)},
                kind=SpanKind.CLIENT,
            ),
            self._graph.session(write=True) as session,
        ):
            session.execute_write(
                lambda tx: tx.run(
                    "UNWIND $rows AS row MERGE (c:Chunk {chunk_id: row.chunk_id}) "
                    "SET c += row.props",
                    rows=rows,
                ).consume()
            )

    def delete_source(
        self, source_id: str, keep: set[str] | frozenset[str] = frozenset()
    ) -> int:
        """Delete a document's chunks except `keep`; return how many were deleted."""
        with self._graph.session(write=True) as session:
            record = session.execute_write(
                lambda tx: tx.run(
                    "MATCH (c:Chunk {source_id: $source_id}) "
                    "WHERE NOT c.chunk_id IN $keep DETACH DELETE c RETURN count(*) AS n",
                    source_id=source_id,
                    keep=sorted(keep),
                ).single(strict=True)
            )
        return int(record["n"])

    # --- reads ------------------------------------------------------------------
    def get_by_ids(self, chunk_ids: Sequence[str]) -> list[ChunkRecord]:
        """Return the stored chunks for these ids, in order; unknown ids are skipped."""
        attributes = {
            "db.system.name": "neo4j",
            "nexo.chunks.requested": len(chunk_ids),
        }
        with (
            traced_span(
                "chunk_store get_by_ids", attributes, kind=SpanKind.CLIENT
            ) as span,
            self._graph.session(write=False) as session,
        ):
            nodes = session.execute_read(
                lambda tx: [
                    dict(r["c"])
                    for r in tx.run(
                        "MATCH (c:Chunk) WHERE c.chunk_id IN $ids AND c.text IS NOT NULL "
                        "RETURN c",
                        ids=list(chunk_ids),
                    )
                ]
            )
            span.set_attribute("nexo.chunks.found", len(nodes))
        by_id = {node["chunk_id"]: _record(node) for node in nodes}
        return [by_id[i] for i in chunk_ids if i in by_id]

    def search(
        self, query: str, k: int, chunk_filter: ChunkFilter
    ) -> list[ChunkRecord]:
        """Top-k chunks the filter allows, ranked by the store's retrieval mode."""
        attributes = retrieval_attributes(k, self._mode) | {"db.system.name": "neo4j"}
        candidates = k if self._mode == "vector" else max(k * 4, 20)
        allowed: dict[str, ChunkRecord] = {}
        fetch = candidates * OVERFETCH
        with (
            traced_span(
                f"retrieval {DATA_SOURCE_ID}", attributes, kind=SpanKind.CLIENT
            ) as span,
            self._graph.session(write=False) as session,
        ):
            rankings: list[list[str]] = []
            if self._mode in ("vector", "hybrid"):
                vector = self._embeddings.embed_query(query)
                rows = session.execute_read(
                    _hits,
                    "CALL db.index.vector.queryNodes($index, $fetch, $vector)" + _HIT,
                    index=VECTOR_INDEX,
                    fetch=fetch,
                    vector=vector,
                )
                rankings.append(_visible(rows, chunk_filter, candidates, allowed))
            terms = tokenize(query)
            if self._mode in ("keyword", "hybrid") and terms:
                rows = session.execute_read(
                    _hits,
                    "CALL db.index.fulltext.queryNodes($index, $terms, {limit: $fetch})"
                    + _HIT,
                    index=TEXT_INDEX,
                    terms=" ".join(terms),
                    fetch=fetch,
                )
                rankings.append(_visible(rows, chunk_filter, candidates, allowed))
            ranked = fuse(rankings)[:k] if rankings else []
            span.set_attribute("nexo.chunks.found", len(ranked))
        return [allowed[chunk_id] for chunk_id in ranked]

    def known_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        """Which of these chunk ids the graph has (with or without text)."""
        with self._graph.session(write=False) as session:
            return set(
                session.execute_read(
                    lambda tx: [
                        r["id"]
                        for r in tx.run(
                            "MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS id",
                            ids=list(chunk_ids),
                        )
                    ]
                )
            )

    def missing_text(self) -> int:
        """Chunks the graph references that have no text here (ingested before ADR-0018)."""
        with self._graph.session(write=False) as session:
            record = session.execute_read(
                lambda tx: tx.run(
                    "MATCH (c:Chunk) WHERE c.text IS NULL RETURN count(c) AS n"
                ).single(strict=True)
            )
        return int(record["n"])


def import_local_store(store: Neo4jChunkStore, path: Path) -> dict[str, int]:
    """Copy a LocalChunkStore JSON file into Neo4j with its stored vectors.

    Nothing is re-embedded, so the import needs no model server. Returns how many chunks
    were imported and how many of them the graph already referenced.
    """
    if not path.exists():
        raise FileNotFoundError(f"no local chunk store at {path}")
    entries = json.loads(path.read_text(encoding="utf-8"))
    chunks = [
        ChunkRecord.model_validate(e["metadata"]["record"]) for e in entries.values()
    ]
    known = store.known_ids([c.chunk_id for c in chunks])
    store.write(
        chunks,
        [e["text"] for e in entries.values()],
        [e["vector"] for e in entries.values()],
    )
    return {"imported": len(chunks), "referenced_by_graph": len(known)}
