"""The Makefile: every documented target works, matches the CLI and repository, and CI uses it."""

from __future__ import annotations

import platform
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from nexo import cli

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text()
TARGETS = re.findall(r"^([a-z0-9-]+):.*## ", MAKEFILE, re.MULTILINE)
# Values for targets that refuse to run without arguments.
ARGS = {"ingest": ["FILE=corpus/memo.md"], "ask": ["Q=How is A connected to B?"], "app": ["ARGS=models --check"]}

REPO_PATH = re.compile(r"\b((?:deploy|examples|tests|site|src)/[\w./-]+\.(?:sh|py|ipynb|example))\b")

pytestmark =pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def make(*args: str, dry_run: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["make", "--no-print-directory", *(["-n"] if dry_run else []), *args]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_every_documented_target_is_phony_and_listed_by_help():
    phony = set(re.search(r"^\.PHONY:(.*?)(?<!\\)\n", MAKEFILE, re.MULTILINE | re.DOTALL).group(1)
                .replace("\\", " ").split())
    assert len(TARGETS) >= 20
    assert set(TARGETS) == phony
    listed = make(dry_run=False).stdout
    for target in TARGETS:
        assert re.search(rf"^  {re.escape(target)} +\S", listed, re.MULTILINE), f"{target} missing from help"


@pytest.mark.parametrize("target", TARGETS)
def test_target_dry_runs_and_uses_existing_paths(target):
    result = make(target, *ARGS.get(target, []))
    assert result.returncode == 0, result.stderr
    for path in REPO_PATH.findall(result.stdout):
        assert (ROOT / path).exists(), f"{target} uses missing {path}"


def test_nexo_commands_in_the_makefile_exist_in_the_cli(capsys):
    commands = re.findall(r"\$\(RUN\)nexo ([a-z-]+)((?: --[a-z-]+)*)", MAKEFILE)
    assert {c for c, _ in commands} >= {"models", "ingest", "ask", "mcp", "graph"}
    for subcommand, flags in commands:
        with pytest.raises(SystemExit):
            cli.main([subcommand, "--help"])
        help_text = capsys.readouterr().out
        for flag in flags.split():
            assert flag in help_text, f"nexo {subcommand} has no {flag}"


def test_tools_come_from_the_virtualenv_unless_run_is_overridden():
    assert make("test").stdout.strip().startswith(".venv/bin/pytest")
    assert make("test", "RUN=").stdout.strip().startswith("pytest")
    assert make("lint", "RUN=", "VENV=elsewhere").stdout.strip() == "ruff check src tests examples"


def test_container_cli_uses_the_linux_override_only_on_linux():
    out = make("app", "ARGS=models --check").stdout
    assert out.strip().endswith("run --rm nexo models --check")
    assert ("docker-compose.linux.yml" in out) == (platform.system() == "Linux")


def test_targets_with_required_arguments_explain_themselves():
    for target, variable in (("ingest", "FILE"), ("ask", "Q")):
        result = make(target, dry_run=False)  # the guard runs before any nexo command
        assert result.returncode != 0
        assert f"usage: make {target} {variable}=" in result.stderr


def test_neo4j_targets_agree_with_the_integration_tests():
    out = make("test-neo4j").stdout
    assert "-p 127.0.0.1:7688:7687" in out and "NEO4J_AUTH=neo4j/nexo-test-pass" in out
    assert "NEO4J_TEST_URI=bolt://127.0.0.1:7688 NEO4J_TEST_PASSWORD=nexo-test-pass" in out
    assert "tests/test_neo4j_integration.py" in out


def test_ci_runs_the_makefile_targets():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    invoked = {shlex.split(line)[1] for line in re.findall(r"run: (make [^\n]+)", workflow)}
    assert invoked == {"lint", "neo4j-test", "test"}


def test_make_targets_mentioned_in_docs_exist():
    texts = [ROOT / "README.md", ROOT / "site" / "index.html", *sorted((ROOT / "docs").rglob("*.md"))]
    mentioned = {t for path in texts for t in re.findall(r"`make ([a-z0-9-]+)", path.read_text())}
    mentioned |= {t for path in texts for line in re.findall(r"`make((?: [a-z0-9-]+)+)`", path.read_text())
                  for t in line.split()}
    mentioned |= {t for path in texts for t in re.findall(r"^make ([a-z0-9-]+)", path.read_text(), re.MULTILINE)}
    assert mentioned, "docs should point at the Makefile"
    assert mentioned <= set(TARGETS), mentioned - set(TARGETS)
