"""The GitHub Pages site (site/index.html) must be self-contained, accessible and match the repository."""

from __future__ import annotations

import re
import shlex
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml

from nexo import cli

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "site" / "index.html"
REPO_BLOB = "https://github.com/sjortiz/nexo/blob/main/"


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.code_blocks: list[str] = []
        self._stack: list[str] = []
        self._code: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, {k: v or "" for k, v in attrs}))
        self._stack.append(tag)
        if tag == "code" and "pre" in self._stack:
            self._code = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "code" and self._code is not None:
            self.code_blocks.append("".join(self._code))
            self._code = None
        if tag in self._stack:
            del self._stack[len(self._stack) - 1 - self._stack[::-1].index(tag) :]

    def handle_data(self, data: str) -> None:
        if self._code is not None:
            self._code.append(data)


@pytest.fixture(scope="module")
def page() -> _Page:
    parser = _Page()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    return parser


def _attrs(page: _Page, tag: str) -> list[dict[str, str]]:
    return [a for t, a in page.tags if t == tag]


def _commands(page: _Page) -> list[list[str]]:
    """Shell commands from the page's code blocks, comments stripped, split on && ."""
    commands = []
    for block in page.code_blocks:
        for line in block.splitlines():
            line = line.split("#", 1)[0].strip()
            for part in filter(None, (p.strip() for p in line.split("&&"))):
                commands.append(shlex.split(part))
    return commands


def test_document_basics(page):
    html = _attrs(page, "html")
    assert html and html[0].get("lang") == "en"
    assert any(a.get("name") == "viewport" for a in _attrs(page, "meta"))
    assert any(a.get("name") == "description" and a.get("content") for a in _attrs(page, "meta"))
    assert re.search(r"<title>[^<]+</title>", PAGE.read_text(encoding="utf-8"))


def test_ids_are_unique_and_every_fragment_resolves(page):
    ids = [a["id"] for _, a in page.tags if "id" in a]
    assert len(ids) == len(set(ids)), "duplicate id"
    fragments = {a["href"][1:] for a in _attrs(page, "a") if a.get("href", "").startswith("#")}
    assert fragments, "expected in-page navigation"
    assert fragments <= set(ids), f"dangling anchors: {fragments - set(ids)}"
    # SVG references (markers, aria) must resolve too.
    refs = set()
    for _, a in page.tags:
        refs |= set(re.findall(r"url\(#([\w-]+)\)", a.get("marker-end", "")))
        refs |= set(a.get("aria-labelledby", "").split())
    assert refs <= set(ids), f"dangling references: {refs - set(ids)}"


def test_repository_links_point_at_existing_files(page):
    links = {unescape(a["href"]) for a in _attrs(page, "a") if a.get("href", "").startswith(REPO_BLOB)}
    assert len(links) >= 5
    missing = [link for link in links if not (ROOT / link[len(REPO_BLOB) :].split("#", 1)[0]).is_file()]
    assert not missing, missing


def test_self_contained(page):
    assert not _attrs(page, "script"), "the page needs no JavaScript"
    assert not [a for a in _attrs(page, "link") if "stylesheet" in a.get("rel", "")]
    for tag, attrs in page.tags:
        for name in ("src", "srcset", "poster"):
            assert not attrs.get(name, "").startswith(("http:", "https:", "//")), (tag, attrs)


def test_images_have_text_alternatives(page):
    for attrs in _attrs(page, "img"):
        assert "alt" in attrs, attrs
    labelled = [a for a in _attrs(page, "svg") if a.get("role") == "img"]
    assert labelled, "the workflow diagram should be exposed as an image"
    for attrs in labelled:
        assert attrs.get("aria-labelledby"), attrs
    for attrs in _attrs(page, "svg"):
        assert attrs.get("role") == "img" or attrs.get("aria-hidden") == "true", attrs


def test_dark_mode_and_license_are_declared():
    text = PAGE.read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in text
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert f'{REPO_BLOB}LICENSE">MIT License' in text


def test_quickstart_cli_flags_exist(page, capsys):
    nexo = [c for c in _commands(page) if c[0] == "nexo"]
    assert {c[1] for c in nexo} >= {"ingest", "ask", "review", "mcp"}
    for command in nexo:
        with pytest.raises(SystemExit):
            cli.main([command[1], "--help"])
        help_text = capsys.readouterr().out
        for flag in (w for w in command[2:] if w.startswith("--")):
            assert flag in help_text, f"{flag} is not an option of nexo {command[1]}"


def test_quickstart_files_settings_and_services_exist(page):
    commands = _commands(page)
    for command in commands:
        for word in command:
            if word.startswith(("deploy/", "docs/")):
                assert (ROOT / word).exists(), word
    env_example = (ROOT / "deploy/local/local.env.example").read_text()
    for key in ("LOCAL_PROFILE", "NEO4J_PASSWORD"):
        assert re.search(rf"^{key}=", env_example, re.MULTILINE), key
    services = set(yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"])
    compose = [c for c in commands if c[:2] == ["podman", "compose"]]
    assert compose
    for command in compose:
        named = [w for w in command[command.index("up") + 1 :] if not w.startswith("-")]
        assert named and set(named) <= services, set(named) - services
