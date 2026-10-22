"""`nexo access`: each document's access levels, why, and what each clearance sees.

A chunk's level is its document's declared level (`nexo ingest --sensitivity`),
raised by the detector when it finds personal data; `sensitivity_reasons` names
what it found. A caller's clearance then decides, per chunk, whether it is shown
as stored, masked (only detected PII put it above the clearance) or withheld,
using the same redactor the agent and MCP tools use. See
docs/operations/access-levels.md.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from nexo.agents.redaction import ChunkRedactor
from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.vector_store import ChunkFilter

_DOCUMENTS = """
MATCH (d:Document) WHERE $source IS NULL OR d.source_id = $source
OPTIONAL MATCH (c:Chunk)-[:PART_OF]->(d)
RETURN d.source_id AS source_id, d.title AS title, d.sensitivity AS declared,
       collect(c.chunk_id) AS chunk_ids
ORDER BY source_id
"""


def visibility(
    record: ChunkRecord, clearance: Sensitivity, redactor: ChunkRedactor
) -> str:
    """How a caller with this clearance sees the chunk: visible, masked or withheld."""
    shown = redactor.visible(record, ChunkFilter(max_sensitivity=clearance))
    if shown is None:
        return "withheld"
    return "visible" if shown.text == record.text else "masked"


def document_access(
    document: Mapping[str, Any],
    records: Iterable[ChunkRecord],
    redactor: ChunkRedactor,
) -> dict[str, Any]:
    """One document's chunk levels, the reasons for them, and each clearance's view."""
    records = list(records)
    sees = {
        clearance.value: dict(
            Counter(visibility(r, clearance, redactor) for r in records)
        )
        for clearance in Sensitivity
    }
    return {
        "source_id": document["source_id"],
        "title": document.get("title") or document["source_id"],
        "declared": document.get("declared"),
        "chunks": dict(Counter(r.sensitivity.value for r in records)),
        "reasons": dict(Counter(why for r in records for why in r.sensitivity_reasons)),
        "sees": sees,
    }


def access_report(
    graph: Any, chunks: Any, redactor: ChunkRedactor, source: str | None = None
) -> list[dict[str, Any]]:
    """`document_access` for every document (or one), from the graph and chunk store."""
    with graph.session(write=False) as session:
        documents = session.run(_DOCUMENTS, source=source).data()
    return [
        document_access(doc, chunks.get_by_ids(doc["chunk_ids"]), redactor)
        for doc in documents
    ]
