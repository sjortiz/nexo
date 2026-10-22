"""Claude Code registration; never write the user's Claude configuration ourselves."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib.resources import files
from pathlib import Path

SCOPES = ("project", "user")
CHECKOUT = Path(__file__).resolve().parents[2]


def skill_text() -> str:
    """Read the unchanged repository skill, or its copy included in a wheel."""
    source = CHECKOUT / ".claude/skills/nexo-knowledge/SKILL.md"
    if source.is_file():
        return source.read_text(encoding="utf-8")
    return (
        files("nexo").joinpath("resources/nexo-knowledge/SKILL.md").read_text("utf-8")
    )


def add_parser(sub, handler) -> None:
    """Keep plain `nexo mcp` serving; registration is an optional subcommand."""
    serve = sub.add_parser("mcp", help="serve or register Nexo MCP tools")
    serve.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--directory",
        type=Path,
        help="load .env, rendered settings and relative data from this directory",
    )
    serve.set_defaults(handler=handler)
    actions = serve.add_subparsers(dest="mcp_action")
    for name in ("install", "uninstall"):
        parser = actions.add_parser(name, help=f"{name} Nexo in Claude Code")
        parser.add_argument("--scope", choices=SCOPES, help="prompt when omitted")
        parser.add_argument(
            "--on-existing",
            choices=("update", "skip"),
            help="explicit replacement policy; otherwise ask, or skip without a terminal",
        )
        if name == "install":
            parser.add_argument(
                "--skill",
                action=argparse.BooleanOptionalAction,
                default=None,
                help="install the skill too (ask; default yes for user scope)",
            )
            parser.add_argument(
                "--directory",
                type=Path,
                help="Nexo settings directory (.env and .nexo); user default: checkout",
            )
            parser.add_argument(
                "--clearance",
                choices=("public", "internal", "pii", "phi"),
                help="pin MCP_CLEARANCE (default: settings in --directory)",
            )
        parser.set_defaults(handler=run)


def _ask(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = " [Y/n] " if default else " [y/N] "
    answer = input(question + suffix).strip().lower()
    return answer in ("y", "yes") if answer else default


def _replace(label: str, policy: str | None) -> bool:
    if policy is not None:
        return policy == "update"
    return _ask(f"{label} already exists. Update it? (No = skip)")


def _read_config(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ValueError(f"invalid MCP configuration: {path}")
    return data


def _server(args: argparse.Namespace, scope: str, project: Path) -> dict:
    if scope == "project":
        return {"command": "uv", "args": ["run", "--quiet", "nexo", "mcp"]}
    directory = (args.directory or project).expanduser().resolve(strict=True)
    if not directory.is_dir():
        raise ValueError(f"not a settings directory: {directory}")
    # uv --project selects the package; --directory below selects Nexo's config and
    # relative data paths before settings/logging are loaded, even in another project.
    if (CHECKOUT / "pyproject.toml").is_file():
        uv = shutil.which("uv")
        if uv is None:
            raise ValueError("uv is required to register this checkout")
        command = str(Path(uv).resolve())
        argv = ["run", "--project", str(CHECKOUT), "--quiet", "nexo"]
    else:
        entry = Path(sys.executable).parent / "nexo"
        if not entry.is_file():
            raise ValueError("install Nexo's entry point before registering MCP")
        command, argv = str(entry.absolute()), []
    # Use a subprocess for resolution: do not change the installer's cwd or settings
    # cache, and use Nexo's normal provider/rendered-env/.env precedence.
    clearance = args.clearance
    if clearance is None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from nexo.config import Settings; "
                "print(Settings().effective_mcp_clearance)",
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        )
        clearance = result.stdout.strip()
    if clearance not in ("public", "internal", "pii", "phi"):
        raise ValueError("could not resolve MCP_CLEARANCE; pass --clearance")
    return {
        "command": command,
        "args": [*argv, "mcp", "--directory", str(directory)],
        "env": {"MCP_CLEARANCE": clearance, "MCP_REVIEW_TOOLS": "false"},
    }


def _command(claude: str, scope: str, server: dict) -> list[str]:
    argv = [claude, "mcp", "add", "--scope", scope]
    for key, value in server.get("env", {}).items():
        argv += ["--env", f"{key}={value}"]
    return [
        *argv,
        "--transport",
        "stdio",
        "nexo",
        "--",
        server["command"],
        *server["args"],
    ]


def _invoke(argv: list[str], cwd: Path) -> None:
    subprocess.run(argv, cwd=cwd, check=True)


def _skill(scope: str, project: Path, args: argparse.Namespace) -> None:
    base = Path.home() if scope == "user" else project
    target = base / ".claude/skills/nexo-knowledge/SKILL.md"
    exists = target.exists() or target.is_symlink()
    if args.mcp_action == "uninstall":
        if not exists:
            return
        # Only remove our known file automatically; preserve locally edited skills.
        if target.is_symlink() or target.read_text(encoding="utf-8") != skill_text():
            if not _replace(str(target), args.on_existing):
                print(f"Skipped modified skill: {target}")
                return
        target.unlink()
        if not any(target.parent.iterdir()):
            target.parent.rmdir()
        print(f"Removed skill: {target}")
        return
    install = args.skill
    if install is None:
        install = _ask("Install the nexo-knowledge skill too?", scope == "user")
    if not install:
        return
    if exists and not _replace(str(target), args.on_existing):
        print(f"Skipped existing skill: {target}")
        return
    # Replacing a symlink must not write through it to another file.
    if target.is_symlink():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(skill_text(), encoding="utf-8")
    print(f"Installed skill: {target}")


def run(args: argparse.Namespace) -> int:
    """Install/remove using Claude's CLI, or emit exact manual instructions."""
    try:
        return _run(args)
    except subprocess.CalledProcessError as exc:
        # A rollback command may contain credentials from the old registration.
        # Report its exit status, never its full argv.
        print(
            f"MCP {args.mcp_action} failed: subprocess exited {exc.returncode}",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError) as exc:
        print(f"MCP {args.mcp_action} failed: {exc}", file=sys.stderr)
        return 2


def _run(args: argparse.Namespace) -> int:
    scope = args.scope
    if scope is None:
        if not sys.stdin.isatty():
            raise ValueError(
                "pass --scope project|user when running without a terminal"
            )
        scope = input("MCP scope (project/user) [project]: ").strip() or "project"
        if scope not in SCOPES:
            raise ValueError("scope must be project or user")
    if (
        args.mcp_action == "install"
        and scope == "project"
        and (args.directory is not None or args.clearance is not None)
    ):
        raise ValueError("--directory and --clearance require --scope user")
    # Detection is read-only. Refuse custom Claude homes rather than risk checking
    # one file and asking Claude to replace an entry in a different configuration.
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        raise ValueError(
            "unset CLAUDE_CONFIG_DIR to use the standard Claude configuration"
        )
    project = Path.cwd()
    config_path = (
        Path.home() / ".claude.json" if scope == "user" else project / ".mcp.json"
    )
    config = _read_config(config_path)
    exists = "nexo" in config.get("mcpServers", {})
    claude = shutil.which("claude")
    removing = args.mcp_action == "uninstall"
    if removing:
        if exists:
            argv = [claude or "claude", "mcp", "remove", "--scope", scope, "nexo"]
            if claude:
                _invoke(argv, project)
            else:
                print("Claude CLI not found. Run:\n" + shlex.join(argv))
        else:
            print(f"No {scope}-scope nexo server to remove.")
    else:
        _install_server(args, scope, project, config, claude)
    _skill(scope, project, args)
    return 0


def _install_server(args, scope, project, config, claude) -> None:
    exists = "nexo" in config.get("mcpServers", {})
    if exists and not _replace(f"{scope}-scope nexo server", args.on_existing):
        print(f"Skipped existing {scope}-scope nexo server.")
        return
    default_directory = CHECKOUT if (CHECKOUT / "pyproject.toml").is_file() else project
    server = _server(args, scope, default_directory)
    argv = _command(claude or "claude", scope, server)
    remove = [claude or "claude", "mcp", "remove", "--scope", scope, "nexo"]
    if claude:
        if exists:
            _invoke(remove, project)
        try:
            _invoke(argv, project)
        except subprocess.CalledProcessError:
            if exists:
                _invoke(
                    [
                        claude,
                        "mcp",
                        "add-json",
                        "--scope",
                        scope,
                        "nexo",
                        json.dumps(config["mcpServers"]["nexo"]),
                    ],
                    project,
                )
                print("Restored the previous nexo registration.", file=sys.stderr)
            raise
    else:
        print("Claude CLI not found. Run:")
        if exists:
            print(shlex.join(remove))
        print(shlex.join(argv))
        print("Server JSON (under mcpServers.nexo):\n" + json.dumps(server, indent=2))
