"""The docs/ bundle must conform to Open Knowledge Format v0.2 and stay in sync with the code."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
import yaml

from nexo.config import Settings

DOCS = Path(__file__).resolve().parent.parent / "docs"
RESERVED = {"index.md", "log.md"}
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FOOTNOTE_REF = re.compile(r"\[\^([^\]]+)\](?!:)")
FOOTNOTE_DEF = re.compile(r"^\[\^([^\]]+)\]:", re.MULTILINE)
FENCE = re.compile(r"```.*?```", re.DOTALL)
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)
ALL_MD = sorted(DOCS.rglob("*.md"))
CONCEPTS = [p for p in ALL_MD if p.name not in RESERVED]


def _split(path: Path) -> tuple[dict | None, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None, text
    _, front, body = text.split("---\n", 2)
    return yaml.safe_load(front), body


def _slug(heading: str) -> str:
    # GitHub-style anchors: lowercase, drop punctuation except - and _, spaces to dashes.
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def _rel(path: Path) -> str:
    return str(path.relative_to(DOCS))


def test_bundle_is_not_empty():
    """The bundle has a root index and at least 20 concept pages."""
    assert (DOCS / "index.md").exists() and len(CONCEPTS) >= 20


@pytest.mark.parametrize("path", CONCEPTS, ids=_rel)
def test_concept_frontmatter(path):
    """Each concept carries OKF frontmatter with valid type, status, title and timestamps."""
    front, _ = _split(path)
    assert isinstance(front, dict), "concepts need a YAML frontmatter block"
    assert (
        isinstance(front.get("type"), str) and front["type"].strip()
    ), "type is required"
    assert front.get("status", "stable") in {"draft", "stable", "deprecated"}
    for key in ("title", "description"):
        assert front.get(key), f"{key} is recommended and used by index.md"
    generated = front.get("generated")
    if generated is not None:
        assert generated.get("by"), "generated.by is required within generated"
        assert (
            isinstance(generated.get("at"), datetime) and generated["at"].tzinfo
        ), "UTC offset required"
    verified = front.get("verified")
    for event in ([verified] if isinstance(verified, dict) else verified or []):
        assert event.get("by") and isinstance(event.get("at"), datetime)
    if "stale_after" in front:
        assert (
            isinstance(front["stale_after"], datetime) and front["stale_after"].tzinfo
        )


@pytest.mark.parametrize("path", CONCEPTS, ids=_rel)
def test_sources_and_footnotes_agree(path):
    """Source ids are unique and every source is cited by exactly one footnote definition."""
    front, body = _split(path)
    sources = front.get("sources") or []
    ids = [s.get("id") for s in sources if s.get("id")]
    assert len(ids) == len(set(ids)), "source ids must be unique"
    for source in sources:
        assert source.get("resource"), "sources[].resource is required"
    referenced = set(FOOTNOTE_REF.findall(body))
    defined = set(FOOTNOTE_DEF.findall(body))
    assert referenced <= set(
        ids
    ), f"footnotes without a source: {referenced - set(ids)}"
    assert defined == referenced, "every footnote needs exactly one definition"
    assert set(ids) <= referenced, f"sources never cited: {set(ids) - referenced}"


def _local_targets(path: Path):
    front, body = _split(path)
    fields = []
    if front:
        fields += [front.get("resource")] + [
            s.get("resource") for s in front.get("sources") or []
        ]
    links = LINK.findall(FENCE.sub("", body))
    for target in [*fields, *links]:
        if target and not re.match(r"^[a-z]+://|^mailto:", target):
            yield target


@pytest.mark.parametrize("path", ALL_MD, ids=_rel)
def test_links_and_resources_resolve(path):
    """Local links and resource paths point at existing files, indexed folders and headings."""
    for target in _local_targets(path):
        file_part, _, anchor = target.partition("#")
        resolved = (path.parent / file_part).resolve() if file_part else path
        assert resolved.exists(), f"{target} does not exist"
        if resolved.is_dir() and resolved.is_relative_to(DOCS):
            assert (resolved / "index.md").exists(), f"{target} has no index.md"
        if anchor:
            _, body = _split(resolved)
            slugs = {_slug(h) for h in HEADING.findall(FENCE.sub("", body))}
            assert anchor in slugs, f"{target}: no heading for #{anchor}"


def test_reserved_files_follow_spec():
    """index.md files list entries in OKF form and log.md files are dated newest first."""
    for index in DOCS.rglob("index.md"):
        front, body = _split(index)
        if index.parent == DOCS:
            assert front == {"okf_version": "0.2"}
        else:
            assert front is None, f"{_rel(index)} must not carry frontmatter"
        assert re.search(
            r"^\* \[[^\]]+\]\([^)]+\) - \S", body, re.MULTILINE
        ), "index entries: * [Title](url) - text"
    for log in DOCS.rglob("log.md"):
        front, body = _split(log)
        assert front is None
        dates = re.findall(r"^## (\S+)$", body, re.MULTILINE)
        assert dates and all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) for d in dates)
        assert dates == sorted(dates, reverse=True), "log entries newest first"


@pytest.mark.parametrize(
    "directory", sorted({p.parent for p in ALL_MD}), ids=lambda d: _rel(d) or "root"
)
def test_every_concept_and_subdirectory_is_indexed(directory):
    """Every concept page and non-empty subdirectory is linked from its folder's index.md."""
    index = directory / "index.md"
    assert index.exists()
    listed = {
        (directory / t.partition("#")[0]).resolve()
        for t in LINK.findall(index.read_text())
    }
    for child in directory.iterdir():
        if child.suffix == ".md" and child.name not in RESERVED:
            assert (
                child.resolve() in listed
            ), f"{_rel(child)} missing from {_rel(index)}"
        if child.is_dir() and any(child.rglob("*.md")):
            assert (
                child.resolve() in listed
            ), f"{_rel(child)}/ missing from {_rel(index)}"


def test_every_setting_is_documented():
    """Every Settings field appears as an env var in the configuration page."""
    page = (DOCS / "operations" / "configuration.md").read_text()
    missing = [
        name.upper()
        for name in Settings.model_fields.keys()
        if f"`{name.upper()}`" not in page
    ]
    assert not missing, f"undocumented settings: {missing}"
