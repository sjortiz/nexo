"""Registration uses a stub Claude CLI and a temporary HOME, never real user files."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from nexo import cli, mcp_install
from nexo.config import get_settings


@pytest.fixture(name="install_env")
def fixture_install_env(tmp_path, monkeypatch):
    """A fake Claude CLI implements only scoped registration, with an invocation log."""
    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "other project"
    project.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "claude"
    stub.write_text(
        f"#!{sys.executable}\n" + """
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["CLAUDE_CALLS"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
scope = args[args.index("--scope") + 1]
path = Path.home() / ".claude.json" if scope == "user" else Path.cwd() / ".mcp.json"
config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"mcpServers": {}}
servers = config.setdefault("mcpServers", {})
action = args[1]
if action == "remove":
    servers.pop("nexo", None)
elif action == "add-json":
    servers["nexo"] = json.loads(args[-1])
else:
    if os.environ.get("CLAUDE_FAIL"):
        sys.exit(1)
    if "nexo" in servers:
        sys.exit(2)
    boundary = args.index("--")
    assert args[boundary - 1] == "nexo"
    env = {}
    for i, value in enumerate(args[:boundary]):
        if value == "--env":
            key, content = args[i + 1].split("=", 1)
            env[key] = content
    servers["nexo"] = {"command": args[boundary + 1], "args": args[boundary + 2:]}
    if env:
        servers["nexo"]["env"] = env
path.write_text(json.dumps(config))
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CLAUDE_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.chdir(project)
    get_settings.cache_clear()
    yield home, project, monkeypatch
    get_settings.cache_clear()
    logging.getLogger().handlers.clear()


def _config(home):
    return json.loads((home / ".claude.json").read_text(encoding="utf-8"))


def _calls():
    path = Path(os.environ["CLAUDE_CALLS"])
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


def test_user_install_launches_from_an_unrelated_project(install_env):
    """The stored command selects Nexo and loads its own settings, never the client .env."""
    home, project, _ = install_env
    settings_dir = home / "Nexo config"
    settings_dir.mkdir()
    (settings_dir / ".env").write_text(
        "MCP_CLEARANCE=pii\nNEO4J_URI=bolt://nexo:7687\nMCP_REVIEW_TOOLS=true\n"
    )
    (project / ".env").write_text("NEO4J_URI=bolt://wrong:7687\nMCP_CLEARANCE=phi\n")
    assert (
        cli.main(
            ["mcp", "install", "--scope", "user", "--directory", str(settings_dir)]
        )
        == 0
    )
    server = _config(home)["mcpServers"]["nexo"]
    assert Path(server["command"]).is_absolute()
    assert server["args"][:4] == [
        "run",
        "--project",
        str(mcp_install.CHECKOUT),
        "--quiet",
    ]
    assert server["env"] == {"MCP_CLEARANCE": "pii", "MCP_REVIEW_TOOLS": "false"}
    assert (home / ".claude/skills/nexo-knowledge/SKILL.md").read_text(
        encoding="utf-8"
    ) == mcp_install.skill_text()
    assert not (project / ".mcp.json").exists()
    # Launch through the registered uv prefix. Replace only the server entry point
    # with a probe that uses its real parser/settings but never connects to Neo4j.
    probe = """
import json
import os
import sys
from nexo import cli
from nexo.config import get_settings
def serve(args):
    s = get_settings()
    print(json.dumps([str(os.getcwd()), s.neo4j_uri, s.effective_mcp_clearance, s.mcp_review_tools]))
    return 0
cli._mcp = serve
sys.exit(cli.main(sys.argv[1:]))
"""
    result = subprocess.run(
        [
            server["command"],
            *server["args"][:4],
            "python",
            "-c",
            probe,
            *server["args"][5:],
        ],
        env=os.environ | server["env"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == [
        str(settings_dir),
        "bolt://nexo:7687",
        "pii",
        False,
    ]


def test_project_install_preserves_current_command_and_other_servers(install_env):
    """Project scope keeps today's command and edits only through Claude."""
    home, project, _ = install_env
    path = project / ".mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    assert cli.main(["mcp", "install", "--scope", "project", "--no-skill"]) == 0
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["mcpServers"]["nexo"] == {
        "command": "uv",
        "args": ["run", "--quiet", "nexo", "mcp"],
    }
    assert data["mcpServers"]["other"] == {"command": "other"}
    assert not (home / ".claude.json").exists()


def test_existing_server_and_skill_require_explicit_update(install_env, capsys):
    """Repeated noninteractive installs skip; explicit update replaces only Nexo."""
    home, _, _ = install_env
    config = {
        "mcpServers": {"nexo": {"command": "old"}, "other": {"command": "keep"}},
        "theme": "dark",
    }
    (home / ".claude.json").write_text(json.dumps(config))
    target = home / ".claude/skills/nexo-knowledge/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("custom skill")
    command = ["mcp", "install", "--scope", "user", "--clearance", "public"]
    assert cli.main(command) == 0
    assert "Skipped existing" in capsys.readouterr().out
    assert _calls() == []
    assert _config(home) == config
    assert target.read_text(encoding="utf-8") == "custom skill"
    assert cli.main(command + ["--on-existing", "update"]) == 0
    assert [call[1] for call in _calls()] == ["remove", "add"]
    assert _config(home)["theme"] == "dark"
    assert _config(home)["mcpServers"]["other"] == {"command": "keep"}
    assert target.read_text(encoding="utf-8") == mcp_install.skill_text()


def test_uninstall_is_scoped_and_removes_the_skill(install_env):
    """User removal leaves the client project's configuration and unrelated skills alone."""
    home, project, _ = install_env
    project_config = project / ".mcp.json"
    project_config.write_text('{"mcpServers":{"nexo":{"command":"project"}}}')
    assert cli.main(["mcp", "install", "--scope", "user", "--clearance", "public"]) == 0
    sibling = home / ".claude/skills/other/SKILL.md"
    sibling.parent.mkdir(parents=True)
    sibling.write_text("keep")
    assert cli.main(["mcp", "uninstall", "--scope", "user"]) == 0
    assert "nexo" not in _config(home)["mcpServers"]
    assert not (home / ".claude/skills/nexo-knowledge").exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
    assert "project" in project_config.read_text(encoding="utf-8")
    assert cli.main(["mcp", "uninstall", "--scope", "user"]) == 0


def test_absent_claude_prints_exact_commands_without_writing_config(
    install_env, capsys
):
    """Fallback output can be parsed as a shell command; .claude.json stays untouched."""
    home, _, patch = install_env
    real_which = mcp_install.shutil.which
    patch.setattr(
        mcp_install.shutil,
        "which",
        lambda name: None if name == "claude" else real_which(name),
    )
    assert (
        cli.main(
            [
                "mcp",
                "install",
                "--scope",
                "user",
                "--no-skill",
                "--clearance",
                "internal",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    command = shlex.split(output.split("Run:\n")[1].splitlines()[0])
    assert command[:5] == ["claude", "mcp", "add", "--scope", "user"]
    assert "MCP_CLEARANCE=internal" in command
    assert not (home / ".claude.json").exists()
    (home / ".claude.json").write_text('{"mcpServers":{"nexo":{"command":"old"}}}')
    before = (home / ".claude.json").read_bytes()
    assert cli.main(["mcp", "uninstall", "--scope", "user"]) == 0
    assert "claude mcp remove --scope user nexo" in capsys.readouterr().out
    assert (home / ".claude.json").read_bytes() == before


def test_prompted_scope_and_default_skill(install_env):
    """Interactive setup asks scope and offers the user skill with default yes."""
    home, _, patch = install_env
    prompts = []
    answers = iter(["user", ""])
    patch.setattr(sys.stdin, "isatty", lambda: True)

    def answer(prompt):
        prompts.append(prompt)
        return next(answers)

    patch.setattr("builtins.input", answer)
    assert cli.main(["mcp", "install", "--clearance", "public"]) == 0
    assert "project/user" in prompts[0]
    assert "[Y/n]" in prompts[1]
    assert (home / ".claude/skills/nexo-knowledge/SKILL.md").exists()


def test_missing_scope_noninteractive_and_bad_config_fail_safely(install_env, capsys):
    """Ambiguous or malformed configuration never causes a Claude mutation."""
    home, _, _ = install_env
    assert cli.main(["mcp", "install"]) == 2
    assert "--scope" in capsys.readouterr().err
    (home / ".claude.json").write_text("broken")
    assert cli.main(["mcp", "install", "--scope", "user"]) == 2
    assert _calls() == []
    assert (home / ".claude.json").read_text(encoding="utf-8") == "broken"


def test_failed_update_restores_registration_and_does_not_touch_skill(install_env):
    """An add failure after removal restores the prior entry through Claude add-json."""
    home, _, patch = install_env
    config = {"mcpServers": {"nexo": {"command": "old"}}}
    (home / ".claude.json").write_text(json.dumps(config))
    patch.setenv("CLAUDE_FAIL", "1")
    assert (
        cli.main(
            [
                "mcp",
                "install",
                "--scope",
                "user",
                "--clearance",
                "public",
                "--on-existing",
                "update",
            ]
        )
        == 2
    )
    assert _config(home) == config
    assert [call[1] for call in _calls()] == ["remove", "add", "add-json"]
    assert not (home / ".claude/skills/nexo-knowledge").exists()


def test_modified_skill_is_preserved_on_uninstall(install_env):
    """Locally edited skill content needs an explicit update policy to remove it."""
    home, _, _ = install_env
    target = home / ".claude/skills/nexo-knowledge/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("mine")
    assert cli.main(["mcp", "uninstall", "--scope", "user"]) == 0
    assert target.read_text(encoding="utf-8") == "mine"
    assert (
        cli.main(["mcp", "uninstall", "--scope", "user", "--on-existing", "update"])
        == 0
    )
    assert not target.exists()


def test_packaged_install_uses_absolute_entry_point(install_env):
    """Without a source checkout the installed interpreter's nexo entry point is used."""
    home, project, patch = install_env
    patch.setattr(mcp_install, "CHECKOUT", home / "no-checkout")
    assert (
        cli.main(
            ["mcp", "install", "--scope", "user", "--no-skill", "--clearance", "public"]
        )
        == 0
    )
    server = _config(home)["mcpServers"]["nexo"]
    assert server["command"] == str(Path(sys.executable).parent / "nexo")
    assert server["args"] == ["mcp", "--directory", str(project)]


def test_user_no_skill_and_independent_skill_install(install_env):
    """Skipping an existing server still permits adding the optional user skill."""
    home, _, _ = install_env
    assert (
        cli.main(
            ["mcp", "install", "--scope", "user", "--no-skill", "--clearance", "public"]
        )
        == 0
    )
    target = home / ".claude/skills/nexo-knowledge/SKILL.md"
    assert not target.exists()
    before = _config(home)
    assert (
        cli.main(
            ["mcp", "install", "--scope", "user", "--skill", "--on-existing", "skip"]
        )
        == 0
    )
    assert _config(home) == before
    assert target.read_text(encoding="utf-8") == mcp_install.skill_text()


def test_interactive_update_and_skip(install_env):
    """Interactive users can decline replacement, then explicitly confirm an update."""
    home, _, patch = install_env
    config = {"mcpServers": {"nexo": {"command": "old"}}}
    (home / ".claude.json").write_text(json.dumps(config))
    patch.setattr(sys.stdin, "isatty", lambda: True)
    patch.setattr("builtins.input", lambda _: "n")
    command = [
        "mcp",
        "install",
        "--scope",
        "user",
        "--no-skill",
        "--clearance",
        "public",
    ]
    assert cli.main(command) == 0
    assert _config(home) == config
    patch.setattr("builtins.input", lambda _: "y")
    assert cli.main(command) == 0
    assert _config(home) != config


def test_project_settings_flags_are_not_silently_ignored(install_env, capsys):
    """The unchanged project command cannot honor the user-only settings options."""
    _, _, _ = install_env
    assert (
        cli.main(["mcp", "install", "--scope", "project", "--clearance", "public"]) == 2
    )
    assert "require --scope user" in capsys.readouterr().err
    assert _calls() == []


def test_custom_claude_home_is_rejected_without_mutation(install_env, capsys):
    """Detection and CLI writes must use the same config location."""
    home, _, patch = install_env
    patch.setenv("CLAUDE_CONFIG_DIR", str(home / "custom"))
    assert cli.main(["mcp", "install", "--scope", "user"]) == 2
    assert "CLAUDE_CONFIG_DIR" in capsys.readouterr().err
    assert _calls() == []


def test_install_does_not_follow_an_existing_skill_symlink(install_env):
    """Explicit skill replacement unlinks a symlink rather than changing its referent."""
    home, _, _ = install_env
    original = home / "original.md"
    original.write_text("keep")
    target = home / ".claude/skills/nexo-knowledge/SKILL.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(original)
    assert (
        cli.main(
            [
                "mcp",
                "install",
                "--scope",
                "user",
                "--skill",
                "--clearance",
                "public",
                "--on-existing",
                "update",
            ]
        )
        == 0
    )
    assert not target.is_symlink()
    assert original.read_text(encoding="utf-8") == "keep"
