"""`nexo setup`: check, render and run the stack a setup file describes.

nexo setup init [--preset mac-single]   write nexo.yaml from a preset, with this machine's profile
nexo setup secrets                      fill missing secrets in the provider
nexo setup lock [--upgrade]             resolve nexo.yaml into nexo.lock
nexo setup render [--addon NAME]        write .nexo/render/
nexo setup check [--no-models]          preflight, then ask each model server
nexo setup up [--addon NAME]            secrets, render, models, compose up (native: no compose)
nexo setup down                         compose down (volumes kept)
nexo setup neo4j-init                   native: give a new Neo4j the stored password
nexo setup compose ARGS...              the container engine's compose on the render
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from nexo.config import Settings
from nexo.llm import ModelRow, build_embeddings, check_model_servers, resolved_models
from nexo.local_profiles import MODEL_SIZES_GB, ProfileError, parse_profile
from nexo.secret_store import SecretStore, SecretStoreError, store_from_options
from nexo.setup.check import (
    this_os,
    FORCEABLE,
    detect_host,
    detect_profile,
    neo4j_login,
    preflight,
    stale_neo4j_hint,
)
from nexo.setup.components import COMPONENTS, UIS
from nexo.setup.envfile import ensure_secrets, read_env, stack_secrets
from nexo.setup.lock import Lock, served_locally
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


def _native(casting: Casting) -> bool:
    return casting.spec.target.mode == "native"


def _ensure_render(args: argparse.Namespace) -> _Stack:
    """The rendered stack with the secrets already in the provider (nothing generated)."""
    casting, lock = _context(args)
    if _native(casting):
        raise SetupError(
            "target.mode native runs no containers: Neo4j is this machine's service"
            " (brew services stop neo4j) and Ollama runs on its own"
        )
    host = detect_host(casting)
    if not (Path.cwd() / RENDER_DIR / "compose.yaml").exists():
        render(casting, lock, Path.cwd(), host)
    return _Stack(host, stack_secrets(store_for(casting), COMPONENTS))


# The local machine's profile line in a preset (models.local.profile).
LOCAL_PROFILE_LINE = re.compile(r"^(\s+local:\n\s+profile:\s*)([\w-]+)", re.MULTILINE)


def _init(args: argparse.Namespace) -> int:
    target = Path(args.file)
    if target.exists() and not args.force:
        log.info("%s exists; keeping it (--force to replace)", target)
        return 0
    detected = None if args.profile else detect_profile()
    preset = args.preset or (
        "mac-single"
        if this_os() == "macos"
        else "linux-gpu" if detected and detected[0].startswith("gpu-") else "linux-cpu"
    )
    source = Path.cwd() / PRESETS / preset / "nexo.yaml"
    if not source.exists():
        names = sorted(p.name for p in (Path.cwd() / PRESETS).iterdir() if p.is_dir())
        raise SetupError(f"no preset {preset!r}; presets: {', '.join(names)}")
    text = source.read_text(encoding="utf-8")
    profile = args.profile or (detected and detected[0])
    if args.profile:
        try:
            parse_profile(args.profile)
        except ProfileError as exc:
            raise SetupError(str(exc)) from exc
    if not LOCAL_PROFILE_LINE.search(text):
        target.write_text(text, encoding="utf-8")
        log.info("wrote %s from preset %s", target, preset)
    elif profile:
        target.write_text(
            LOCAL_PROFILE_LINE.sub(rf"\g<1>{profile}", text, count=1), encoding="utf-8"
        )
        how = f"detected: {detected[1]}" if detected else "--profile"
        log.info(
            "wrote %s from preset %s with profile %s (%s; --profile to choose another)",
            target,
            preset,
            profile,
            how,
        )
    else:
        target.write_text(text, encoding="utf-8")
        log.warning(
            "wrote %s from preset %s; this machine's profile is unknown: set"
            " models.local.profile (e.g. m1-max-64, gpu-24, cpu-64) or pass --profile",
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
    on = sorted(name for name, enabled in lock.enabled.items() if enabled)
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
    casting: Casting, lock: Lock, on: set[str], host: Host, force: bool
) -> bool:
    """Log the preflight; False when a check failed (memory ones pass with --force)."""
    findings = preflight(casting, lock, on, host, store_for(casting))
    blocked = False
    for finding in findings:
        forced = not finding.ok and (
            finding.advisory or (force and finding.name in FORCEABLE)
        )
        status = "ok  " if finding.ok else "WARN" if forced else "FAIL"
        (log.info if finding.ok else log.warning)(
            "%s %s: %s%s",
            status,
            finding.name,
            finding.detail,
            " (--force)" if forced and not finding.advisory else "",
        )
        blocked = blocked or (not finding.ok and not forced)
    if blocked and any(not f.ok and f.name in FORCEABLE for f in findings):
        log.warning("to go ahead anyway: --force (make ... FORCE=1)")
    return not blocked


def model_lines(rows: list[ModelRow]) -> list[tuple[bool, str]]:
    """One (ok, text) per role; a role that is off (no model) is ok and says why."""
    lines = []
    for row in rows:
        if not row.model:
            lines.append((True, f"model {row.role}: {row.note or 'off'}"))
            continue
        status = row.check or "ok"
        text = f"model {row.role}: {row.model} at {row.base_url}"
        if status != "ok":
            text += f" ({status})"
            if row.served is not None:
                text += f"; the server has: {', '.join(row.served) or 'no models'}"
            if row.detail:
                text += f"; {row.detail}"
        lines.append((status == "ok", text))
    return lines


def pulled_by_up(casting: Casting, lock: Lock) -> set[str]:
    """Models `up` pulls into this machine's Ollama (not a remote machine's)."""
    models = casting.spec.models
    if models.provider != "ollama":
        return set()
    return served_locally(lock, models)


def _models_reachable(pending: set[str] = frozenset()) -> bool:
    """Ask each role's server for its model, as `nexo models --check` does.

    A model in pending that the server lacks is not a failure: `up` pulls it.
    """
    settings = Settings()  # fresh: render just rewrote nexo.env
    rows = check_model_servers(settings, resolved_models(settings))
    later = [
        row
        for row in rows
        if row.check == "model not served here" and row.model in pending
    ]
    for row in later:
        log.info(
            "todo model %s: %s not pulled yet; up pulls it (%s GB)",
            row.role,
            row.model,
            MODEL_SIZES_GB.get(row.model, "?"),
        )
    lines = model_lines([row for row in rows if row not in later])
    for ok, text in lines:
        (log.info if ok else log.warning)("%s %s", "ok  " if ok else "FAIL", text)
    return all(ok for ok, _ in lines)


def _check(args: argparse.Namespace) -> int:
    casting, lock = _context(args)
    host = detect_host(casting)
    render(casting, lock, Path.cwd(), host, _addons(args))
    ok = _report(casting, lock, enabled_set(lock, _addons(args)), host, args.force)
    if not args.no_models:
        ok = _models_reachable(pulled_by_up(casting, lock)) and ok
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


def carry_checkpoints(root: Path) -> None:
    """Copy a checkout's old .data/checkpoints.sqlite to the stack's, if it has none (ADR-0018)."""
    settings = Settings()  # fresh: render just rewrote nexo.env
    old, new = root / ".data/checkpoints.sqlite", settings.checkpoint_sqlite_path
    if settings.checkpoint_backend != "sqlite" or not old.is_file():
        return
    if new.exists():  # never overwrite: it may hold other checkouts' reviews
        log.info("checkpoints: left %s in place, %s already exists", old, new)
        return
    tmp = Path(f"{new}.tmp")
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        # a WAL left by an unclean exit holds recent writes; it goes first so the
        # main file never appears without it
        if (wal := Path(f"{old}-wal")).is_file():
            shutil.copy2(wal, f"{new}-wal")
        shutil.copy2(old, tmp)  # then a rename: a cut-short copy never looks finished
        os.replace(tmp, new)
    except OSError as exc:  # a stack that can't take the copy still starts
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        log.warning("checkpoints: could not copy %s to %s: %s", old, new, exc)
        return
    log.info("checkpoints: copied %s to %s (the old file is kept)", old, new)


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
    carry_checkpoints(Path.cwd())
    if casting.spec.models.provider == "ollama" and not args.skip_models:
        if code := _start_models(casting):
            return code
    if _native(casting):  # the preflight logged in to this machine's Neo4j
        log.info(
            "native: Neo4j at %s, nothing in containers; run nexo in the .venv"
            " (nexo config --explain shows where each setting comes from)",
            ports.bolt,
        )
        return 0
    if code := (
        _compose(stack, UP)
        or _wait_for_services(stack)
        or _neo4j_accepts(stack, store, lock.name, ports.bolt)
    ):
        return code
    for name in sorted(on & UIS.keys()):
        log.info("%s: http://localhost:%s", name, getattr(ports, UIS[name]))
    if "agentInbox" in on:
        log.info(
            "agentInbox: http://127.0.0.1:%s (add inbox: graph nexo, URL http://127.0.0.1:%s); "
            "start the adapter with make review-inbox",
            ports.agent_inbox,
            ports.review_inbox,
        )
    return 0


def _down(args: argparse.Namespace) -> int:
    return _compose(_ensure_render(args), ["down", "--remove-orphans"])


def _passthrough(args: argparse.Namespace) -> int:
    return _compose(_ensure_render(args), args.compose_args)


def _neo4j_init(args: argparse.Namespace) -> int:
    """target.mode native: set a new Neo4j's initial password to the stored one.

    neo4j-admin takes the password as an argument only, so it is briefly visible to this
    machine's process list; it is never logged.
    """
    casting, _ = _context(args)
    if not _native(casting):
        raise SetupError(
            "neo4j-init is for target.mode native; the Neo4j container gets the stored"
            " password when it is created"
        )
    store = store_for(casting)
    password = ensure_secrets(Path.cwd() / ".env", Path.cwd(), {"neo4j"}, store)[
        "NEO4J_PASSWORD"
    ]
    admin = shutil.which("neo4j-admin")
    if admin is None:
        raise SetupError("neo4j-admin not found: brew install neo4j (or your package)")
    code = subprocess.run(
        [admin, "dbms", "set-initial-password", password], check=False
    ).returncode
    if code == 0:
        log.info(
            "Neo4j's initial password is the one in %s; start it: brew services start"
            " neo4j (Linux: sudo systemctl start neo4j), then make check",
            store.label,
        )
    else:
        log.error(
            "neo4j-admin failed; it only works before Neo4j's first start (stop it first)"
        )
    return code


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
    init.add_argument(
        "--profile",
        help="models.local.profile (default: detected from the chip and memory)",
    )
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
    verb(
        "neo4j-init",
        _neo4j_init,
        "target.mode native: set a new Neo4j's initial password to the stored one",
    )
    verb("compose", _passthrough, "run compose on the rendered stack").add_argument(
        "compose_args", nargs=argparse.REMAINDER
    )
