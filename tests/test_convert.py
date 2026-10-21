"""PDF, Word and HTML become Markdown before ingestion; headings survive."""

import importlib.util
from pathlib import Path

import pytest

from nexo.storage import convert
from nexo.storage.chunking import split_document
from nexo.storage.convert import ConversionError, to_markdown
from nexo.storage.schemas import DocumentMetadata

FIXTURES = Path(__file__).parent / "fixtures" / "formats"
# find_spec, not importorskip: importing markitdown here would run its load_dotenv().
needs_markitdown = pytest.mark.skipif(
    importlib.util.find_spec("markitdown") is None, reason="extra formats not installed"
)
BODY = "Delta Devices designs wearable glucose monitors."


@needs_markitdown
def test_converting_leaves_the_environment_alone(tmp_path, monkeypatch):
    """markitdown's import-time load_dotenv must not leak .env into os.environ."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("NEXO_LEAK_PROBE=1\n", encoding="utf-8")
    monkeypatch.delitem(__import__("sys").modules, "markitdown", raising=False)
    to_markdown(FIXTURES / "profile.html")
    assert "NEXO_LEAK_PROBE" not in __import__("os").environ


@needs_markitdown
@pytest.mark.parametrize("suffix", ["html", "docx", "pdf"])
def test_each_format_converts_to_markdown_with_its_text(suffix):
    """The fixture's heading and paragraph come through MarkItDown."""
    text = to_markdown(FIXTURES / f"profile.{suffix}")
    assert "Delta Devices" in text and BODY in text


@needs_markitdown
@pytest.mark.parametrize("suffix", ["html", "docx"])
def test_headings_survive_so_chunks_keep_their_section(suffix):
    """A converted heading is a Markdown heading, so the chunker records it."""
    text = to_markdown(FIXTURES / f"profile.{suffix}")
    chunks = split_document(
        text, DocumentMetadata(source_id="p"), chunk_size=1200, chunk_overlap=150
    )
    assert chunks and chunks[0].section_path[0] == "Delta Devices"


def test_markdown_passes_through_and_unknown_formats_are_refused(tmp_path):
    """.md is read as is; an unsupported suffix names what is supported."""
    note = tmp_path / "note.md"
    note.write_text("# Note\n\ntext", encoding="utf-8")
    assert to_markdown(note) == "# Note\n\ntext"
    with pytest.raises(ConversionError, match="unsupported format .xlsx"):
        to_markdown(tmp_path / "sheet.xlsx")


def test_a_missing_extra_says_how_to_install_it(monkeypatch):
    """Without markitdown the error names the extra instead of an ImportError."""
    monkeypatch.setitem(__import__("sys").modules, "markitdown", None)
    with pytest.raises(ConversionError, match="uv sync --extra formats"):
        convert.to_markdown(FIXTURES / "profile.html")
