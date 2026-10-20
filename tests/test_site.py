"""The GitHub Pages site (site/*.html) is self-contained, accessible and matches the repo."""

from __future__ import annotations

import re
import shlex
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest

from nexo import cli
from nexo import local_profiles as lp

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
PAGE = SITE / "index.html"
PAGES = sorted(SITE.glob("*.html"))
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


def _parse(path: Path) -> _Page:
    parser = _Page()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


@pytest.fixture(name="page", scope="module")
def fixture_page() -> _Page:
    """The landing page: quickstart checks apply to it."""
    return _parse(PAGE)


@pytest.fixture(name="any_page", scope="module", params=PAGES, ids=lambda p: p.name)
def fixture_any_page(request) -> tuple[Path, _Page]:
    """Every page on the site: structure, links and self-containment."""
    return request.param, _parse(request.param)


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


def _ids(page: _Page) -> set[str]:
    return {a["id"] for _, a in page.tags if "id" in a}


def test_site_has_the_expected_pages():
    """The site consists of exactly the landing page and the walkthrough."""
    assert [p.name for p in PAGES] == ["index.html", "walkthrough.html"]


def test_document_basics(any_page):
    """Each page declares lang, a viewport, a description and a title."""
    path, page = any_page
    html = _attrs(page, "html")
    assert html and html[0].get("lang") == "en"
    assert any(a.get("name") == "viewport" for a in _attrs(page, "meta"))
    assert any(
        a.get("name") == "description" and a.get("content")
        for a in _attrs(page, "meta")
    )
    assert re.search(r"<title>[^<]+</title>", path.read_text(encoding="utf-8"))


def test_ids_are_unique_and_every_fragment_resolves(any_page):
    """Element ids are unique and in-page anchors and SVG references all resolve."""
    _, page = any_page
    ids = [a["id"] for _, a in page.tags if "id" in a]
    assert len(ids) == len(set(ids)), "duplicate id"
    fragments = {
        a["href"][1:] for a in _attrs(page, "a") if a.get("href", "").startswith("#")
    }
    assert fragments, "expected in-page navigation"
    assert fragments <= set(ids), f"dangling anchors: {fragments - set(ids)}"
    # SVG references (markers, aria) must resolve too.
    refs = set()
    for _, a in page.tags:
        refs |= set(re.findall(r"url\(#([\w-]+)\)", a.get("marker-end", "")))
        refs |= set(a.get("aria-labelledby", "").split())
    assert refs <= set(ids), f"dangling references: {refs - set(ids)}"


def test_links_between_pages_resolve(any_page):
    """Links to sibling pages hit existing files and existing fragment ids."""
    _, page = any_page
    for href in (a.get("href", "") for a in _attrs(page, "a")):
        if re.match(r"^[\w-]+\.html", href):
            name, _, fragment = href.partition("#")
            assert (SITE / name).is_file(), href
            if fragment:
                assert fragment in _ids(_parse(SITE / name)), href


def test_repository_links_point_at_existing_files(any_page):
    """GitHub blob links point at files that exist in this repository."""
    path, page = any_page
    links = {
        unescape(a["href"])
        for a in _attrs(page, "a")
        if a.get("href", "").startswith(REPO_BLOB)
    }
    assert len(links) >= (5 if path == PAGE else 2)
    missing = [
        link
        for link in links
        if not (ROOT / link[len(REPO_BLOB) :].split("#", 1)[0]).is_file()
    ]
    assert not missing, missing


def test_self_contained(any_page):
    """Pages load no JavaScript, external stylesheets or remote media."""
    _, page = any_page
    assert not _attrs(page, "script"), "the page needs no JavaScript"
    assert not [a for a in _attrs(page, "link") if "stylesheet" in a.get("rel", "")]
    for tag, attrs in page.tags:
        for name in ("src", "srcset", "poster"):
            assert not attrs.get(name, "").startswith(("http:", "https:", "//")), (
                tag,
                attrs,
            )


def test_images_have_text_alternatives(any_page):
    """Images have alt text and SVGs are either labelled images or aria-hidden."""
    path, page = any_page
    for attrs in _attrs(page, "img"):
        assert "alt" in attrs, attrs
    labelled = [a for a in _attrs(page, "svg") if a.get("role") == "img"]
    if path == PAGE:
        assert labelled, "the workflow diagram should be exposed as an image"
    for attrs in labelled:
        assert attrs.get("aria-labelledby"), attrs
    for attrs in _attrs(page, "svg"):
        assert attrs.get("role") == "img" or attrs.get("aria-hidden") == "true", attrs


def test_dark_mode_and_license_are_declared(any_page):
    """Each page supports dark mode and links the repository's MIT LICENSE."""
    path, _ = any_page
    text = path.read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in text
    assert (ROOT / "LICENSE").read_text().startswith("MIT License")
    assert f'{REPO_BLOB}LICENSE">MIT License' in text


def _nexo_argv(commands: list[list[str]]) -> list[list[str]]:
    """Arguments of every direct nexo invocation."""
    return [c[1:] for c in commands if c[0] == "nexo"]


def test_quickstart_cli_flags_exist(page, capsys):
    """Every nexo subcommand and flag in the quickstart exists in the CLI help."""
    argv = _nexo_argv(_commands(page))
    assert {a[0] for a in argv} >= {"ingest", "ask", "review"}
    for args in argv:
        with pytest.raises(SystemExit):
            cli.main([args[0], "--help"])
        help_text = capsys.readouterr().out
        for flag in (w for w in args[1:] if w.startswith("--")):
            assert flag in help_text, f"{flag} is not an option of nexo {args[0]}"


def test_quickstart_files_targets_and_presets_exist(page):
    """Quickstart paths, make targets, presets and the template's secrets all exist."""
    commands = _commands(page)
    for command in commands:
        for word in command:
            if word.startswith(("deploy/", "docs/", "src/")):
                assert (ROOT / word).exists(), word
    makefile = (ROOT / "Makefile").read_text()
    targets = set(re.findall(r"^([a-z0-9-]+):", makefile, re.MULTILINE))
    make = [c for c in commands if c[0] == "make"]
    assert {"env", "up", "check"} <= {w for c in make for w in c[1:]}
    for command in make:
        for word in command[1:]:
            name, _, value = word.partition("=")
            if not value:
                assert name in targets, f"make {name} is not a target"
            elif name == "PRESET":
                assert (ROOT / "examples/setups" / value / "nexo.yaml").exists(), value
            else:
                assert re.search(rf"^{name}\s*\?=", makefile, re.MULTILINE), name
    env_example = (ROOT / "deploy/local/local.env.example").read_text()
    for key in ("NEO4J_PASSWORD", "SIGNOZ_USER_PASSWORD"):
        assert re.search(rf"^{key}=", env_example, re.MULTILINE), key


def test_quickstart_profiles_and_role_sets_are_valid(page):
    """Profiles and role sets passed to the setup tools in the quickstart are valid."""
    uses = []
    for command in _commands(page):
        if command[0].endswith("ollama-setup.sh"):
            uses.append(command[1:])
        elif command[:2] == ["python3", "src/nexo/local_profiles.py"]:
            uses.append(command[3:])
    assert uses
    for args in uses:
        if args:
            lp.plan(*args)  # raises on an unknown profile or role set
