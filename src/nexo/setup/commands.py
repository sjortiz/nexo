"""`nexo setup`: check, render and run the stack a setup file describes.

nexo setup init [--preset mac-single]   write nexo.yaml from a preset
nexo setup secrets                      fill missing secrets in .env
nexo setup lock [--upgrade]             resolve nexo.yaml into nexo.lock
nexo setup render [--addon NAME]        write .nexo/render/
nexo setup check [--no-models]          preflight, then ask each model server
nexo setup up [--addon NAME]            secrets, render, models, compose up
nexo setup down                         compose down (volumes kept)
nexo setup compose ARGS...              the container engine's compose on the render
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nexo.config import Settings
from nexo.llm import build_embeddings, check_model_servers, resolved_models
from nexo.secret_store import SecretStore, SecretStoreError, store_from_options
from nexo.setup.check import (
    FORCEABLE,
    detect_host,
    neo4j_login,
    preflight,
    stale_neo4j_hint,
)
from nexo.setup.components import COMPONENTS, UIS
from nexo.setup.envfile import ensure_secrets, read_env, stack_secrets
from nexo.setup.lock import lock as resolve_lock
from nexo.setup.render import (
    RENDER_DIR,
    Host,
    enabled_set,
    portable_files,
    render,
    secrets_options,
    write_files,
)
from nexo.setup.schema import ADDONS, Casting, SetupError, load_casting
from nexo.setup.status import assess, parse_ps, stale_volume_hint
from nexo.storage.graph_store import GraphStore
from nexo.storage.neo4j_chunk_store import Neo4jChunkStore

log = logging.getLogger(__name__)
PRESETS = "examples/setups"
# Containers of add-ons a previous run enabled are orphans of this render: remove them.
UP = ["up", "-d", "--remove-orphans"]
# How long up waits for every service to run (images are pulled before this starts).
WAIT_S = 300


def _context(args: argparse.Namespace):
    casting_path = Path(args.file)
    casting = load_casting(casting_path)
    lock = resolve_lock(
        casting, casting_path, Path.cwd(), upgrade=getattr(args, "upgrade", False)
    )
    return casting, lock


def _addons(args: argparse.Namespace) -> frozenset[str]:
    return frozenset(getattr(args, "addon", None) or ())


def store_for(casting: Casting) -> SecretStore:
    """The secrets provider the setup file chose (~/.config/nexo/config by default)."""
    return store_from_options(secrets_options(casting))


@dataclass(frozen=True)
class _Stack:
    """A rendered stack and its secrets, which compose gets through the environment."""

    host: Host
    secrets: dict[str, str] = field(default_factory=dict)

    def command(self, args: list[str]) -> list[str]:
        """The engine's compose on the render, with .env for personal settings."""
        root = Path.cwd()
        command = [
            self.host.engine,
            "compose",
            "-f",
            str(root / RENDER_DIR / "compose.yaml"),
        ]
        if (root / ".env").exists():
            command += ["--env-file", str(root / ".env")]
        return command + args

    def environment(self) -> dict[str, str]:
        """This process's environment plus the secrets; nothing is written to disk."""
        return os.environ | self.secrets


def _compose(stack: _Stack, args: list[str]) -> int:
    command = stack.command(args)
    log.info("%s", " ".join(command))
    return subprocess.run(command, env=stack.environment(), check=False).returncode


def _captured(stack: _Stack, args: list[str], *, stderr: bool = True) -> str:
    result = subprocess.run(
        stack.command(args),
        env=stack.environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "") + ((result.stderr or "") if stderr else "")


def _explain(stack: _Stack, failed: list[str]) -> None:
    """Each failed service's last log lines, and a fix when it is a stale data volume."""
    compose = yaml.safe_load((Path.cwd() / RENDER_DIR / "compose.yaml").read_text())
    for service in failed:
        logs = _captured(stack, ["logs", "--tail", "20", service])
        log.error("%s did not start; its last log lines:\n%s", service, logs.rstrip())
        if hint := stale_volume_hint(service, logs, compose, stack.host.engine):
            log.error("%s", hint)


def _wait_for_services(stack: _Stack) -> int:
    """Until every service runs (one-shot jobs may exit 0); 1 when one fails or hangs."""
    deadline = time.monotonic() + WAIT_S
    while True:
        ps = _captured(stack, ["ps", "--all", "--format", "json"], stderr=False)
        failed, waiting = assess(parse_ps(ps))
        if failed:
            _explain(stack, failed)
            return 1
        if not waiting:
            return 0
        if time.monotonic() > deadline:
            log.error("still starting after %ss: %s", WAIT_S, ", ".join(waiting))
            return 1
        time.sleep(3)


def _neo4j_accepts(stack: _Stack, store: SecretStore, name: str, uri: str) -> int:
    """0 when Neo4j takes the provider's password; its healthcheck only probes HTTP."""
    password = stack.secrets.get("NEO4J_PASSWORD")
    if not password:
        return 0
    user = read_env(Path.cwd() / ".env").get("NEO4J_USER") or "neo4j"
    for _ in range(10):
        result = neo4j_login(uri, user, password)
        if result != "unreachable":
            break
        time.sleep(2)
    if result is None:
        return 0
    if result == "refused":
        log.error("%s", stale_neo4j_hint(store, stack.host, name))
    else:
        log.error("Neo4j does not answer at %s", uri)
    return 1


def _ensure_render(args: argparse.Namespace) -> _Stack:
    """The rendered stack with the secrets already in the provider (nothing generated)."""
    casting, lock = _context(args)
    host = detect_host(casting)
    if not (Path.cwd() / RENDER_DIR / "compose.yaml").exists():
        render(casting, lock, Path.cwd(), host)
    return _Stack(host, stack_secrets(store_for(casting), COMPONENTS))


def _init(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if target.exists() and not args.force:
        log.info("%s exists; keeping it (--force to replace)", target)
        return 0
    preset = args.preset or (
        "mac-single" if platform.system() == "Darwin" else "linux-cpu"
    )
    source = Path.cwd() / PRESETS / preset / "nexo.yaml"
    if not source.exists():
        names = sorted(p.name for p in (Path.cwd() / PRESETS).iterdir() if p.is_dir())
        raise SetupError(f"no preset {preset!r}; presets: {', '.join(names)}")
    shutil.copyfile(source, target)
    log.info(
        "wrote %s from preset %s: set models.local.profile for this machine",
        target,
        preset,
    )
    return 0


def _secrets(args: argparse.Namespace) -> int:
    if not Path(args.file).exists():
        raise SetupError(f"{args.file} not found; create one with `nexo setup init`")
    casting, lock = _context(args)
    store = store_for(casting)
    ensure_secrets(
        Path.cwd() / ".env", Path.cwd(), enabled_set(lock, _addons(args)), store
    )
    log.info("secrets for stack %s: %s", casting.metadata.name, store.label)
    return 0


def _lock(args: argparse.Namespace) -> int:
    _, lock = _context(args)
    on = sorted(name for name, enabled in lock["enabled"].items() if enabled)
    log.info("%s: %s", Path(args.file).with_suffix(".lock"), ", ".join(on))
    return 0


def _render(args: argparse.Namespace) -> int:
    if args.portable:  # the committed docker-compose.yml: fragment defaults, no lock
        files = portable_files(load_casting(Path(args.file)), Path.cwd())
        write_files(Path.cwd(), files)
        log.info("wrote %s", ", ".join(files))
        return 0
    casting, lock = _context(args)
    out = render(casting, lock, Path.cwd(), detect_host(casting), _addons(args))
    log.info("rendered %s", out)
    return 0


def _report(
    casting: Casting, lock: dict, on: set[str], host: Host, force: bool
) -> bool:
    """Log the preflight; False when a check failed (memory ones pass with --force)."""
    findings = preflight(casting, lock, on, host, store_for(casting))
    blocked = False
    for finding in findings:
        forced = not finding.ok and force and finding.name in FORCEABLE
        status = "ok  " if finding.ok else "WARN" if forced else "FAIL"
        (log.info if finding.ok else log.warning)(
            "%s %s: %s%s",
            status,
            finding.name,
            finding.detail,
            " (--force)" if forced else "",
        )
        blocked = blocked or (not finding.ok and not forced)
    if blocked and any(not f.ok and f.name in FORCEABLE for f in findings):
        log.warning("to go ahead anyway: --force (make ... FORCE=1)")
    return not blocked


def model_lines(rows: list[dict]) -> list[tuple[bool, str]]:
    """One (ok, text) per role; a role that is off (no model) is ok and says why."""
    lines = []
    for row in rows:
        if not row.get("model"):
            lines.append((True, f"model {row['role']}: {row.get('note', 'off')}"))
            continue
        status = row.get("check", "ok")
        text = f"model {row['role']}: {row['model']} at {row['base_url']}"
        if status != "ok":
            text += f" ({status})"
            if "served" in row:
                text += f"; the server has: {', '.join(row['served']) or 'no models'}"
            if row.get("detail"):
                text += f"; {row['detail']}"
        lines.append((status == "ok", text))
    return lines


def _models_reachable() -> bool:
    """Ask each role's server for its model, as `nexo models --check` does."""
    settings = Settings()  # fresh: render just rewrote nexo.env
    lines = model_lines(check_model_servers(settings, resolved_models(settings)))
    for ok, text in lines:
        (log.info if ok else log.warning)("%s %s", "ok  " if ok else "FAIL", text)
    return all(ok for ok, _ in lines)


def _check(args: argparse.Namespace) -> int:
    casting, lock = _context(args)
    host = detect_host(casting)
    render(casting, lock, Path.cwd(), host, _addons(args))
    ok = _report(casting, lock, enabled_set(lock, _addons(args)), host, args.force)
    if not args.no_models:
        ok = _models_reachable() and ok
    return 0 if chunks_have_text() and ok else 1


def chunks_have_text() -> bool:
    """With the Neo4j chunk store, every chunk the graph cites has its text (ADR-0018)."""
    settings = Settings()  # fresh: render just rewrote nexo.env
    if settings.chunk_store != "neo4j":
        return True
    password = settings.neo4j_password.get_secret_value()
    if neo4j_login(settings.neo4j_uri, settings.neo4j_user, password) is not None:
        return True  # not running (or refused: the neo4j finding said so)
    graph = GraphStore.from_settings(settings, read_only=True)
    try:
        store = Neo4jChunkStore(
            graph, build_embeddings(settings), dimensions=settings.embedding_dimensions
        )
        missing = store.missing_text()
    finally:
        graph.close()
    if missing:
        log.warning(
            "FAIL chunks: %d chunks in the graph have no text (answers cannot cite them):"
            " nexo chunks import (from %s), or re-ingest",
            missing,
            settings.vector_store_path,
        )
        return False
    log.info("ok   chunks: every chunk in the graph has its text")
    return True


def _start_models(casting: Casting) -> int:
    models = casting.spec.models
    roles = "light" if models.remote is not None else "single"
    script = Path.cwd() / "deploy/local/ollama-setup.sh"
    return subprocess.run(
        [str(script), models.local.profile, roles], check=False
    ).returncode


def _up(args: argparse.Namespace) -> int:
    casting, lock = _context(args)
    host = detect_host(casting)
    on = enabled_set(lock, _addons(args))
    store = store_for(casting)
    stack = _Stack(host, ensure_secrets(Path.cwd() / ".env", Path.cwd(), on, store))
    ports = casting.spec.ports
    render(casting, lock, Path.cwd(), host, _addons(args))
    if not _report(casting, lock, on, host, args.force):
        return 1
    if casting.spec.models.provider == "ollama" and not args.skip_models:
        if code := _start_models(casting):
            return code
    if code := (
        _compose(stack, UP)
        or _wait_for_services(stack)
        or _neo4j_accepts(stack, store, lock["name"], ports.bolt)
    ):
        return code
    for name in sorted(on & UIS.keys()):
        log.info("%s: http://localhost:%s", name, getattr(ports, UIS[name]))
    return 0


def _down(args: argparse.Namespace) -> int:
    return _compose(_ensure_render(args), ["down", "--remove-orphans"])


def _passthrough(args: argparse.Namespace) -> int:
    return _compose(_ensure_render(args), args.compose_args)


def run(args: argparse.Namespace) -> int:
    """Run one `nexo setup` verb; setup problems are reported, not raised."""
    try:
        return args.verb_handler(args)
    except (SetupError, SecretStoreError) as exc:
        log.error("%s", exc)
        return 2


def add_parser(
    sub: argparse._SubParsersAction,
) -> None:
    """Register `nexo setup` and its verbs."""
    setup = sub.add_parser(
        "setup", help="render and run the stack described by nexo.yaml"
    )
    setup.add_argument(
        "-f", "--file", default="nexo.yaml", help="setup file (default nexo.yaml)"
    )
    setup.set_defaults(handler=run)
    verbs = setup.add_subparsers(dest="verb", required=True)

    def verb(name: str, handler, help_text: str, *, addons: bool = False):
        parser = verbs.add_parser(name, help=help_text)
        parser.set_defaults(verb_handler=handler)
        if addons:
            parser.add_argument(
                "--addon",
                action="append",
                choices=ADDONS,
                help="also enable for this run",
            )
        return parser

    init = verb("init", _init, "write nexo.yaml from a preset in examples/setups")
    init.add_argument("--preset")
    init.add_argument("--force", action="store_true")
    verb("secrets", _secrets, "fill missing secrets in .env for the enabled components")
    verb("lock", _lock, "resolve nexo.yaml into nexo.lock").add_argument(
        "--upgrade", action="store_true", help="move every pin to the current defaults"
    )
    verb("render", _render, "write .nexo/render/", addons=True).add_argument(
        "--portable",
        action="store_true",
        help="write docker-compose.yml and deploy/rendered/ (repo-relative paths)",
    )
    check = verb("check", _check, "preflight and model server checks", addons=True)
    check.add_argument(
        "--no-models", action="store_true", help="skip asking the model servers"
    )
    up = verb("up", _up, "start the stack (and Ollama's models)", addons=True)
    up.add_argument(
        "--skip-models", action="store_true", help="do not run ollama-setup.sh"
    )
    for parser in (check, up):
        parser.add_argument(
            "--force",
            action="store_true",
            help="go ahead although the memory or profile checks fail",
        )
    verb("down", _down, "stop the stack; volumes are kept")
    verb("compose", _passthrough, "run compose on the rendered stack").add_argument(
        "compose_args", nargs=argparse.REMAINDER
    )
