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
from nexo import local_profiles as lp

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


def _compose(command: list[str]) -> tuple[list[str], str, list[str]]:
    """Files (-f), verb (up/run) and the words after the verb of a `podman compose` command."""
    files, verb_at = [], None
    for i, word in enumerate(command[2:], start=2):
        if command[i - 1] == "-f":
            files.append(word)
        elif word in ("up", "run"):
            verb_at = i
            break
    assert verb_at, command
    return files or ["docker-compose.yml"], command[verb_at], command[verb_at + 1 :]


def _nexo_argv(commands: list[list[str]]) -> list[list[str]]:
    """Arguments of every nexo invocation: direct, or inside `compose run ... nexo`."""
    argv = [c[1:] for c in commands if c[0] == "nexo"]
    for command in commands:
        if command[:2] == ["podman", "compose"]:
            _, verb, rest = _compose(command)
            words = [w for w in rest if not w.startswith("-")]
            if verb == "run" and words[0] == "nexo":
                argv.append(rest[rest.index("nexo") + 1 :])
    return argv


def test_quickstart_cli_flags_exist(page, capsys):
    argv = _nexo_argv(_commands(page))
    assert {a[0] for a in argv} >= {"ingest", "ask", "review", "models"}
    for args in argv:
        with pytest.raises(SystemExit):
            cli.main([args[0], "--help"])
        help_text = capsys.readouterr().out
        for flag in (w for w in args[1:] if w.startswith("--")):
            assert flag in help_text, f"{flag} is not an option of nexo {args[0]}"


def test_quickstart_files_settings_and_services_exist(page):
    commands = _commands(page)
    for command in commands:
        for word in command:
            if word.startswith(("deploy/", "docs/", "src/")):
                assert (ROOT / word).exists(), word
    env_example = (ROOT / "deploy/local/local.env.example").read_text()
    for key in ("LOCAL_PROFILE", "NEO4J_PASSWORD", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        assert re.search(rf"^{key}=", env_example, re.MULTILINE), key
    compose = [c for c in commands if c[:2] == ["podman", "compose"]]
    assert {_compose(c)[1] for c in compose} == {"up", "run"}
    for command in compose:
        files, verb, rest = _compose(command)
        services = set()
        for name in files:
            assert (ROOT / name).is_file(), name
            services |= set(yaml.safe_load((ROOT / name).read_text())["services"])
        named = [w for w in rest if not w.startswith("-")]
        targets = named if verb == "up" else named[:1]
        assert targets and set(targets) <= services, set(targets) - services


def test_quickstart_profiles_and_role_sets_are_valid(page):
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
