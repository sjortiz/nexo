"""Secrets for the enabled components: kept in the secrets provider, never in a file to copy.

`nexo setup` generates what is missing into the provider (~/.config/nexo/config by default)
and hands the values to compose through the process environment (local only). `.env` keeps personal
settings and keys only you can provide (HF_TOKEN, LANGSMITH_API_KEY); generated secrets
found there from older setups move into the provider and are blanked in `.env`.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from nexo.secret_store import SecretStore, parse_dotenv
from nexo.setup.components import COMPONENTS
from nexo.setup.schema import SetupError

log = logging.getLogger(__name__)
TEMPLATE = "deploy/local/local.env.example"


def _hex(n: int) -> Callable[[], str]:
    return lambda: secrets.token_hex(n)


# How each generated key looks; anything not listed is 16 random bytes in hex.
GENERATORS: dict[str, Callable[[], str]] = {
    # SigNoz wants upper case, lower case, a digit and a symbol.
    "SIGNOZ_USER_PASSWORD": lambda: f"Nexo-{secrets.token_hex(12)}-1",
    "SIGNOZ_JWT_SECRET": _hex(32),
    "LANGFUSE_PUBLIC_KEY": lambda: f"pk-lf-{secrets.token_hex(16)}",
    "LANGFUSE_SECRET_KEY": lambda: f"sk-lf-{secrets.token_hex(24)}",
    "LANGFUSE_USER_PASSWORD": _hex(12),
    "LANGFUSE_ENCRYPTION_KEY": _hex(32),  # 64 hex characters, as Langfuse requires
    "LANGFUSE_NEXTAUTH_SECRET": _hex(32),
}


def read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of an env file (comments and blank lines skipped)."""
    return parse_dotenv(path.read_text(encoding="utf-8")) if path.exists() else {}


def generated_keys(components: Iterable[str]) -> list[str]:
    """The secrets `nexo setup` generates for these components, in a stable order."""
    keys = ["NEO4J_PASSWORD"] + [k for c in components for k in COMPONENTS[c].secrets]
    return list(dict.fromkeys(keys))


def ensure_dot_env(env_path: Path, root: Path) -> None:
    """.env from the template when there is none; only its owner can read it."""
    if not env_path.exists():
        shutil.copyfile(root / TEMPLATE, env_path)
        log.info("created %s from %s", env_path, TEMPLATE)
    env_path.chmod(0o600)


def _blank(env_path: Path, keys: Iterable[str]) -> None:
    text = env_path.read_text(encoding="utf-8")
    for key in keys:
        text = re.sub(rf"^{key}=.*$", f"{key}=", text, flags=re.MULTILINE)
    env_path.write_text(text, encoding="utf-8")


def ensure_secrets(
    env_path: Path, root: Path, components: Iterable[str], store: SecretStore
) -> dict[str, str]:
    """Every generated secret the components need, creating the missing ones in the store."""
    ensure_dot_env(env_path, root)
    needed = generated_keys(components)
    dot_env = read_env(env_path)
    found = store.get(needed)
    if not store.writable:  # mounted by the platform: it provides every key
        missing = [key for key in needed if key not in found]
        if missing:
            raise SetupError(
                f"{store.label} is read-only and lacks {', '.join(missing)}"
            )
        return found
    moved = {k: dot_env[k] for k in needed if k not in found and dot_env.get(k)}
    created = {
        k: GENERATORS.get(k, _hex(16))()
        for k in needed
        if k not in found and k not in moved
    }
    if moved or created:
        store.put(moved | created)
    if moved:
        _blank(env_path, moved)
        log.info("moved %s from .env to %s", ", ".join(moved), store.label)
    if created:
        log.info("generated %s in %s", ", ".join(created), store.label)
    return found | moved | created


def stack_secrets(store: SecretStore, components: Iterable[str]) -> dict[str, str]:
    """The secrets already in the store, for compose commands that create nothing."""
    return store.get(generated_keys(components))


def missing_required(env_path: Path, components: Iterable[str]) -> list[str]:
    """User-provided keys (e.g. HF_TOKEN) that enabled components need but nobody set."""
    current = read_env(env_path)
    return [
        key
        for c in components
        for key in COMPONENTS[c].required
        if not current.get(key) and not os.environ.get(key)
    ]
