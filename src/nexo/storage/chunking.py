"""Structure-aware chunking.

Applies the AWS "writing best practices for RAG" guidance on the ingestion
side: split on headings so chunks follow document sections, keep the heading
path as metadata, flatten tables into self-describing bullets, and attach
acronym definitions. `lint_document` reports what authors should fix at the
source (no headings, tables, oversized sections, images/links).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from nexo.storage.schemas import ChunkRecord, DocumentMetadata

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_IMAGE_OR_LINK = re.compile(r"!?\[[^\]]*\]\([^)]+\)")


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def flatten_markdown_tables(text: str) -> tuple[str, int]:
    """Rewrite Markdown tables as 'col=value' bullets; returns (text, tables)."""
    lines = text.splitlines()
    out: list[str] = []
    tables = 0
    i = 0
    while i < len(lines):
        if (
            "|" in lines[i]
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR.match(lines[i + 1])
        ):
            headers = _cells(lines[i])
            i += 2
            row_number = 0
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                row_number += 1
                pairs = [
                    f"{h}={v}"
                    for h, v in zip(headers, _cells(lines[i]), strict=False)
                    if v
                ]
                out.append(f"- Row {row_number}: " + "; ".join(pairs))
                i += 1
            tables += 1
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out), tables


def glossary_notes(text: str, glossary: Mapping[str, str]) -> str:
    """Definitions of the glossary terms the text uses, or an empty string."""
    used = [
        f"{term} = {meaning}"
        for term, meaning in glossary.items()
        if re.search(rf"\b{re.escape(term)}\b", text)
    ]
    return ("Definitions: " + "; ".join(used)) if used else ""


def lint_document(text: str, *, max_section_chars: int = 6000) -> list[str]:
    """Warn about document traits that hurt chunking and retrieval."""
    warnings: list[str] = []
    if not _HEADING.search(text):
        warnings.append(
            "no headings: chunks fall back to size-based splits and lose section context"
        )
    if _IMAGE_OR_LINK.search(text):
        warnings.append(
            "images/links present: describe them in text; URLs consume retrieval tokens"
        )
    for section in re.split(r"^#{1,6}\s.*$", text, flags=re.MULTILINE):
        if len(section) > max_section_chars:
            warnings.append(
                f"section over {max_section_chars} chars: split into self-contained sections"
            )
            break
    return warnings


def split_document(
    text: str, metadata: DocumentMetadata, *, chunk_size: int, chunk_overlap: int
) -> list[ChunkRecord]:
    """Heading-aware split, then size-bounded split within each section."""
    sections = MarkdownHeaderTextSplitter(
        headers_to_split_on=_HEADERS, strip_headers=True
    ).split_text(text)
    sizer = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )
    ingested_at = datetime.now(UTC)
    records: list[ChunkRecord] = []
    for section in sections:
        path = [section.metadata[key] for _, key in _HEADERS if key in section.metadata]
        for piece in sizer.split_text(section.page_content):
            if not piece.strip():
                continue
            ordinal = len(records)
            records.append(
                ChunkRecord(
                    chunk_id=ChunkRecord.make_id(metadata.source_id, ordinal, piece),
                    source_id=metadata.source_id,
                    ordinal=ordinal,
                    text=piece,
                    section_path=path,
                    title=metadata.title,
                    doc_type=metadata.doc_type,
                    source_uri=metadata.source_uri,
                    effective_date=metadata.effective_date,
                    sensitivity=metadata.sensitivity,
                    ingested_at=ingested_at,
                )
            )
    return records
