"""Chunk text and embeddings on the graph's own (:Chunk) nodes (ADR-0018).

The graph and its chunks live in one database, so they cannot drift apart:
every checkout of a stack sees both, and deleting a document removes both.
Search is hybrid like the local store: a vector index (cosine) and a full-text
index (Lucene, accents folded) fused with Reciprocal Rank Fusion. Candidates are
filtered with the same `ChunkFilter.allows` as the local store, so clearance rules
live in one place.

Neo4j 2026.09+ reads both indexes with the SEARCH clause; older servers (5.26)
with the `db.index.*.queryNodes` procedures, which 2026.x deprecates.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from datetime import date, datetime
from functools import cached_property
from pathlib import Path
from typing import Any, LiteralString, NamedTuple

from langchain_core.embeddings import Embeddings
from neo4j import ManagedTransaction
from neo4j.exceptions import Neo4jError
from opentelemetry.trace import SpanKind

from nexo.storage.graph_store import GraphStore
from nexo.storage.schemas import ChunkRecord
from nexo.storage.vector_store import (
    DATA_SOURCE_ID,
    ChunkFilter,
    RetrievalMode,
    candidate_count,
    fuse,
    retrieval_attributes,
    tokenize,
)
from nexo.telemetry import traced_span

logger = logging.getLogger(__name__)

VECTOR_INDEX = "chunk_embedding"
TEXT_INDEX = "chunk_search_text"
# Hits are filtered after the index lookup: ask for more than k, then filter.
OVERFETCH = 10

# The first Neo4j release whose SEARCH clause reads full-text as well as vector indexes.
SEARCH_CLAUSE_SINCE = (2026, 9)


class IndexQueries(NamedTuple):
    """Vector and full-text lookups; both take $lookup (vector or terms) and $fetch."""

    vector: LiteralString
    text: LiteralString


# Node properties for a hit, without the embedding and search text (large, unused).
_HIT = (
    " WITH c, score WHERE c.text IS NOT NULL"
    " RETURN c {.*, embedding: null, search_text: null} AS c ORDER BY score DESC"
)
_SEARCH_CLAUSE = IndexQueries(
    vector=f"CYPHER 25 MATCH (c:Chunk) SEARCH c IN (VECTOR INDEX {VECTOR_INDEX}"
    " FOR $lookup LIMIT $fetch) SCORE AS score" + _HIT,
    text=f"CYPHER 25 MATCH (c:Chunk) SEARCH c IN (FULLTEXT INDEX {TEXT_INDEX}"
    " FOR $lookup LIMIT $fetch) SCORE AS score" + _HIT,
)
_PROCEDURES = IndexQueries(
    vector=f"CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $fetch, $lookup)"
    " YIELD node AS c, score" + _HIT,
    text=f"CALL db.index.fulltext.queryNodes('{TEXT_INDEX}', $lookup, {{limit: $fetch}})"
    " YIELD node AS c, score" + _HIT,
)
_COMPONENTS = "CALL dbms.components() YIELD name, versions RETURN name, versions"
_UPSERT = (
    "UNWIND $rows AS row MERGE (c:Chunk {chunk_id: row.chunk_id}) SET c += row.props"
)
_DELETE_SOURCE = (
    "MATCH (c:Chunk {source_id: $source_id}) "
    "WHERE NOT c.chunk_id IN $keep DETACH DELETE c RETURN count(*) AS n"
)
_GET_BY_IDS = "MATCH (c:Chunk) WHERE c.chunk_id IN $ids AND c.text IS NOT NULL RETURN c"
_KNOWN_IDS = "MATCH (c:Chunk) WHERE c.chunk_id IN $ids RETURN c.chunk_id AS id"
_MISSING_TEXT = "MATCH (c:Chunk) WHERE c.text IS NULL RETURN count(c) AS n"
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
    "declared_sensitivity",
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


def _rows(
    tx: ManagedTransaction, cypher: LiteralString, **params: Any
) -> list[dict[str, Any]]:
    """Run one statement and return its records as dicts (nodes as property maps)."""
    return [record.data() for record in tx.run(cypher, **params)]


def index_queries(components: list[dict[str, Any]]) -> IndexQueries:
    """SEARCH on Neo4j 2026.09+, the procedures otherwise, from `dbms.components()` rows.

    A kernel version it cannot read gets the procedures, which every supported server has.
    """
    kernel = [row["versions"] for row in components if row["name"] == "Neo4j Kernel"]
    version = kernel[0][0] if kernel and kernel[0] else None
    match = re.match(r"(\d+)\.(\d+)", version or "")
    if match is None:
        logger.warning(
            "unreadable Neo4j version %r: chunk search uses db.index.*.queryNodes",
            version,
        )
        return _PROCEDURES
    major, minor = int(match[1]), int(match[2])
    return _SEARCH_CLAUSE if (major, minor) >= SEARCH_CLAUSE_SINCE else _PROCEDURES


def _visible(
    rows: list[dict[str, Any]], chunk_filter: ChunkFilter, limit: int
) -> dict[str, ChunkRecord]:
    """The hits the caller may see (ChunkFilter.allows) by id, best first, at most `limit`."""
    visible: dict[str, ChunkRecord] = {}
    for row in rows:
        if len(visible) == limit:
            break
        record = _record(row["c"])
        if chunk_filter.allows(record):
            visible[record.chunk_id] = record
    return visible


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
            session.execute_write(_rows, _UPSERT, rows=rows)

    def delete_source(
        self, source_id: str, keep: set[str] | frozenset[str] = frozenset()
    ) -> int:
        """Delete a document's chunks except `keep`; return how many were deleted."""
        with self._graph.session(write=True) as session:
            [row] = session.execute_write(
                _rows, _DELETE_SOURCE, source_id=source_id, keep=sorted(keep)
            )
        return int(row["n"])

    # --- reads ------------------------------------------------------------------
    @cached_property
    def _queries(self) -> IndexQueries:
        """The index lookups this server supports, decided once per store."""
        try:
            with self._graph.session(write=False) as session:
                components = session.execute_read(_rows, _COMPONENTS)
        except Neo4jError as exc:  # e.g. dbms.components() not granted to this user
            logger.warning(
                "dbms.components() failed (%s): chunk search uses db.index.*.queryNodes",
                exc.code,
            )
            return _PROCEDURES
        return index_queries(components)

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
            rows = session.execute_read(_rows, _GET_BY_IDS, ids=list(chunk_ids))
            span.set_attribute("nexo.chunks.found", len(rows))
        by_id = {row["c"]["chunk_id"]: _record(row["c"]) for row in rows}
        return [by_id[i] for i in chunk_ids if i in by_id]

    def search(
        self, query: str, k: int, chunk_filter: ChunkFilter
    ) -> list[ChunkRecord]:
        """Top-k chunks the filter allows, ranked by the store's retrieval mode."""
        attributes = retrieval_attributes(k, self._mode) | {"db.system.name": "neo4j"}
        candidates = candidate_count(k, self._mode)
        fetch = candidates * OVERFETCH
        queries = self._queries
        with (
            traced_span(
                f"retrieval {DATA_SOURCE_ID}", attributes, kind=SpanKind.CLIENT
            ) as span,
            self._graph.session(write=False) as session,
        ):
            hits: list[dict[str, ChunkRecord]] = []
            if self._mode in ("vector", "hybrid"):
                rows = session.execute_read(
                    _rows,
                    queries.vector,
                    lookup=self._embeddings.embed_query(query),
                    fetch=fetch,
                )
                hits.append(_visible(rows, chunk_filter, candidates))
            terms = tokenize(query)
            if self._mode in ("keyword", "hybrid") and terms:
                rows = session.execute_read(
                    _rows, queries.text, lookup=" ".join(terms), fetch=fetch
                )
                hits.append(_visible(rows, chunk_filter, candidates))
            ranked = fuse([list(found) for found in hits])[:k] if hits else []
            span.set_attribute("nexo.chunks.found", len(ranked))
        allowed = {chunk_id: r for found in hits for chunk_id, r in found.items()}
        return [allowed[chunk_id] for chunk_id in ranked]

    def known_ids(self, chunk_ids: Sequence[str]) -> set[str]:
        """Which of these chunk ids the graph has (with or without text)."""
        with self._graph.session(write=False) as session:
            rows = session.execute_read(_rows, _KNOWN_IDS, ids=list(chunk_ids))
        return {row["id"] for row in rows}

    def missing_text(self) -> int:
        """Chunks the graph references that have no text here (ingested before ADR-0018)."""
        with self._graph.session(write=False) as session:
            [row] = session.execute_read(_rows, _MISSING_TEXT)
        return int(row["n"])


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
