"""Turn PDF, Word and HTML files into Markdown before chunking.

Markdown and text files pass through unchanged. Everything else goes through
MarkItDown (extra `nexo[formats]`), or Docling when INGEST_CONVERTER=docling and
the `docling` package is installed. Both keep headings as `#` lines, so the
heading-aware chunker still splits by section; tables come out as Markdown
tables, which the document linter flattens.
"""

from __future__ import annotations

from pathlib import Path

PASSTHROUGH = {".md", ".markdown", ".txt"}
CONVERTED = {".pdf", ".docx", ".html", ".htm"}


class ConversionError(RuntimeError):
    """A file that cannot be turned into Markdown here (unsupported or missing extra)."""


def to_markdown(path: Path, converter: str = "markitdown") -> str:
    """The file's text as Markdown."""
    suffix = path.suffix.lower()
    if suffix in PASSTHROUGH:
        return path.read_text(encoding="utf-8")
    if suffix not in CONVERTED:
        raise ConversionError(
            f"{path.name}: unsupported format {suffix or '(none)'}; "
            f"supported: {', '.join(sorted(PASSTHROUGH | CONVERTED))}"
        )
    if converter == "docling":
        return _docling(path)
    return _markitdown(path)


def _markitdown(path: Path) -> str:
    try:
        from markitdown import MarkItDown  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ConversionError(
            f"{path.name}: converting {path.suffix} needs the extra: "
            "uv sync --extra formats (or pip install 'nexo[formats]')"
        ) from exc
    return MarkItDown().convert(str(path)).text_content


def _docling(path: Path) -> str:
    try:
        # pylint: disable-next=import-outside-toplevel,import-error
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise ConversionError(
            f"{path.name}: INGEST_CONVERTER=docling needs the docling package "
            "(uv pip install docling)"
        ) from exc
    return DocumentConverter().convert(str(path)).document.export_to_markdown()
