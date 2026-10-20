"""Secrets in `.env`: generated for the enabled components, never overwritten.

`.env` stays the only secret store; the setup file, the lock and the rendered files only
reference these keys.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
from collections.abc import Callable, Iterable
from pathlib import Path

from nexo.setup.components import COMPONENTS

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
    if not path.exists():
        return {}
    pairs = (
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {key.strip(): value for key, value in pairs}


def ensure_secrets(env_path: Path, root: Path, components: Iterable[str]) -> list[str]:
    """Fill missing or empty secrets of the given components; return the keys generated."""
    if not env_path.exists():
        shutil.copyfile(root / TEMPLATE, env_path)
        log.info("created %s from %s", env_path, TEMPLATE)
    env_path.chmod(0o600)
    text = env_path.read_text(encoding="utf-8")
    current = read_env(env_path)
    keys = ["NEO4J_PASSWORD"] + [k for c in components for k in COMPONENTS[c].secrets]
    generated = []
    for key in dict.fromkeys(keys):
        if current.get(key):
            continue
        value = GENERATORS.get(key, _hex(16))()
        line = f"{key}={value}"
        if re.search(rf"^{key}=$", text, re.MULTILINE):
            text = re.sub(rf"^{key}=$", line, text, count=1, flags=re.MULTILINE)
        else:
            text += ("" if text.endswith("\n") or not text else "\n") + line + "\n"
        generated.append(key)
    env_path.write_text(text, encoding="utf-8")
    for key in generated:
        log.info("generated %s", key)
    return generated


def missing_required(env_path: Path, components: Iterable[str]) -> list[str]:
    """User-provided keys (e.g. HF_TOKEN) that enabled components need but .env lacks."""
    current = read_env(env_path)
    return [
        key
        for c in components
        for key in COMPONENTS[c].required
        if not current.get(key)
    ]
