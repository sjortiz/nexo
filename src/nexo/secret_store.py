"""Where Nexo's generated secrets live: a provider, never a file you copy between machines.

    file      (default) ~/.config/nexo/config: one [stack] section per stack, readable only by
              you (0600 in a 0700 directory); every checkout of a stack on the machine shares
              it. Pointed at a file or directory your platform mounts (a Kubernetes Secret,
              the Secrets Store CSI driver, a file CI writes), it is read-only.
    keychain  the OS keychain through keyring (macOS Keychain, Linux Secret Service), opt-in:
              the `keychain` extra (uv sync --extra keychain); nothing else imports keyring.
    sops      a SOPS-encrypted dotenv file (age, PGP or cloud KMS keys), opt-in.
    vault     HashiCorp Vault or OpenBao, KV version 2, opt-in.

`nexo setup` renders the choice into .nexo/render/nexo.env (NEXO_SECRETS_*), and both
`nexo setup` and Nexo itself build the same store from it. Values are never logged.
"""

from __future__ import annotations

import configparser
import importlib
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

PROVIDERS = ("file", "keychain", "sops", "vault")


class SecretStoreError(RuntimeError):
    """The provider could not be read or written; the message says why (never a value)."""


def config_dir() -> Path:
    """Per-user Nexo configuration: NEXO_CONFIG_DIR, $XDG_CONFIG_HOME/nexo or ~/.config/nexo."""
    if explicit := os.environ.get("NEXO_CONFIG_DIR"):
        return Path(explicit)
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "nexo"


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=VALUE lines; comments and blank lines skipped."""
    pairs = (
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    return {key.strip(): value.strip() for key, value in pairs}


def format_dotenv(values: Mapping[str, str]) -> str:
    """The inverse of parse_dotenv, keys sorted."""
    return "".join(f"{key}={values[key]}\n" for key in sorted(values))


class SecretStore(ABC):
    """Reads and (unless read-only) writes named secrets for one stack."""

    label = "secret store"
    writable = True

    @abstractmethod
    def get(self, keys: Iterable[str]) -> dict[str, str]:
        """The keys that have a value."""

    @abstractmethod
    def put(self, values: Mapping[str, str]) -> None:
        """Store values, keeping the others."""


def default_secrets_file() -> Path:
    """The machine's own secrets file, written by `nexo setup`."""
    return config_dir() / "config"


class FileStore(SecretStore):
    """An INI file with a [stack] section, or a directory with one file per key.

    The default, ~/.config/nexo/config, is this machine's own (like ~/.aws/credentials):
    `nexo setup` writes it, owner-only. Any other path is mounted by your platform (a
    Kubernetes Secret volume, the Secrets Store CSI driver, a file CI writes): read-only,
    with whatever permissions the platform sets.
    """

    def __init__(self, path: Path, stack: str):
        self.path = path
        self.stack = stack
        self.writable = path == default_secrets_file()
        self.label = f"{path} [{stack}]" if not path.is_dir() else f"{path}/"

    def _read(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keep NEO4J_PASSWORD as written
        try:
            if self.path.exists():
                parser.read_string(self.path.read_text(encoding="utf-8"))
        except (OSError, configparser.Error) as exc:
            raise SecretStoreError(
                f"cannot read {self.path}: {type(exc).__name__}"
            ) from exc
        return parser

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        if not self.path.exists():
            if self.writable:
                return {}  # nexo setup creates it
            raise SecretStoreError(
                f"secrets file {self.path} not found (is it mounted?)"
            )
        # One file per key, as a Kubernetes Secret volume or the CSI driver mounts them.
        if self.path.is_dir():
            return self._read_directory(keys)
        parser = self._read()
        if not parser.has_section(self.stack):
            return {}
        section = parser[self.stack]
        return {key: section[key] for key in keys if section.get(key)}

    def _read_directory(self, keys: Iterable[str]) -> dict[str, str]:
        found = {}
        for key in keys:
            entry = self.path / key
            try:
                value = (
                    entry.read_text(encoding="utf-8").strip() if entry.is_file() else ""
                )
            except OSError as exc:
                raise SecretStoreError(
                    f"cannot read {entry}: {type(exc).__name__}"
                ) from exc
            if value:
                found[key] = value
        return found

    def put(self, values: Mapping[str, str]) -> None:
        if not self.writable:
            raise SecretStoreError(
                f"{self.label} is mounted read-only: add {', '.join(sorted(values))}"
                " where your platform manages its secrets"
            )
        parser = self._read()
        if not parser.has_section(self.stack):
            parser.add_section(self.stack)
        for key, value in values.items():
            parser[self.stack][key] = value
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self.path.open("w", encoding="utf-8") as handle:
            parser.write(handle)


def system_keychain() -> Any:
    """This machine's keychain through keyring, imported only here (the optional extra)."""
    try:
        keyring = importlib.import_module("keyring")
        fail = importlib.import_module("keyring.backends.fail")
    except ImportError as exc:
        raise SecretStoreError(
            "secrets.provider keychain needs the keychain extra: uv sync --extra keychain"
        ) from exc
    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring):
        raise SecretStoreError("no keychain on this machine: use secrets.provider file")
    return backend


class KeychainStore(SecretStore):
    """The OS keychain, one entry per key under the service "nexo/<stack>" (local use)."""

    # A locked keychain can wait for an unlock prompt nobody answers.
    timeout_s = 10.0

    def __init__(self, stack: str, backend: Any = None):
        """backend: anything with keyring's get/set_password (tests pass a fake)."""
        self.service = f"nexo/{stack}"
        self.label = f"keychain ({self.service})"
        self.backend = backend if backend is not None else system_keychain()

    def _call(self, func: Callable[[], Any]) -> Any:
        """Run one keychain call in a daemon thread, so a stuck call cannot block Nexo."""
        outcome: dict[str, Any] = {}

        def target() -> None:
            # Relays whatever the backend raised (keyring's types need keyring imported) to
            # the caller below, as ThreadPoolExecutor does; its threads would block exit.
            try:
                outcome["value"] = func()
            # pylint: disable-next=broad-exception-caught
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(self.timeout_s)
        if thread.is_alive():
            raise SecretStoreError(
                f"{self.label}: no answer in {self.timeout_s:.0f}s (is it locked?)"
            )
        if "error" in outcome:
            error = outcome["error"]
            raise SecretStoreError(f"{self.label}: {type(error).__name__}") from error
        return outcome.get("value")

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        found = {
            key: self._call(
                lambda key=key: self.backend.get_password(self.service, key)
            )
            for key in keys
        }
        return {key: value for key, value in found.items() if value}

    def put(self, values: Mapping[str, str]) -> None:
        for key, value in values.items():
            self._call(
                lambda key=key, value=value: self.backend.set_password(
                    self.service, key, value
                )
            )


class SopsStore(SecretStore):
    """A SOPS-encrypted dotenv file; encryption keys come from .sops.yaml creation rules."""

    def __init__(self, path: Path):
        self.path = path
        self.label = f"sops ({path})"

    def _sops(self, *args: str, stdin: str | None = None) -> str:
        try:
            result = subprocess.run(
                ["sops", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SecretStoreError("sops is not installed") from exc
        if result.returncode:
            reason = (result.stderr.strip().splitlines() or ["failed"])[-1]
            raise SecretStoreError(f"{self.label}: {reason}")
        return result.stdout

    def _all(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return parse_dotenv(
            self._sops(
                "--decrypt",
                "--input-type",
                "dotenv",
                "--output-type",
                "dotenv",
                str(self.path),
            )
        )

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        stored = self._all()
        return {key: stored[key] for key in keys if stored.get(key)}

    def put(self, values: Mapping[str, str]) -> None:
        plaintext = format_dotenv(self._all() | dict(values))
        encrypted = self._sops(
            "--encrypt",
            "--input-type",
            "dotenv",
            "--output-type",
            "dotenv",
            "--filename-override",
            str(self.path),
            "/dev/stdin",
            stdin=plaintext,
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(encrypted, encoding="utf-8")


class VaultStore(SecretStore):
    """HashiCorp Vault or OpenBao, KV version 2: one secret holding every key."""

    def __init__(self, address: str, mount: str, path: str, token_env: str):
        self.url = f"{address.rstrip('/')}/v1/{mount}/data/{path}"
        self.token_env = token_env
        self.label = f"vault ({mount}/{path})"

    def _request(self, body: dict | None = None) -> dict:
        token = os.environ.get(self.token_env)
        if not token:
            raise SecretStoreError(f"{self.label}: set {self.token_env}")
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"X-Vault-Token": token, "Content-Type": "application/json"},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and body is None:
                return {}
            raise SecretStoreError(f"{self.label}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SecretStoreError(f"{self.label}: unreachable") from exc

    def _all(self) -> dict[str, str]:
        return (self._request().get("data") or {}).get("data") or {}

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        stored = self._all()
        return {key: stored[key] for key in keys if stored.get(key)}

    def put(self, values: Mapping[str, str]) -> None:
        self._request({"data": self._all() | dict(values)})


def store_from_options(options: Mapping[str, str]) -> SecretStore:
    """The store NEXO_SECRETS_* options describe (rendered into nexo.env by `nexo setup`)."""
    stack = options.get("NEXO_STACK") or "nexo"
    provider = options.get("NEXO_SECRETS_PROVIDER") or "file"
    if provider == "file":
        path = options.get("NEXO_SECRETS_FILE")
        return FileStore(Path(path) if path else default_secrets_file(), stack)
    if provider == "sops":
        return SopsStore(Path(options.get("NEXO_SECRETS_SOPS_FILE") or "nexo.sops.env"))
    if provider == "vault":
        address = options.get("NEXO_SECRETS_VAULT_ADDR") or os.environ.get("VAULT_ADDR")
        if not address:
            raise SecretStoreError("vault: set secrets.vault.address or VAULT_ADDR")
        return VaultStore(
            address,
            options.get("NEXO_SECRETS_VAULT_MOUNT") or "secret",
            options.get("NEXO_SECRETS_VAULT_PATH") or f"nexo/{stack}",
            options.get("NEXO_SECRETS_VAULT_TOKEN_ENV") or "VAULT_TOKEN",
        )
    if provider != "keychain":
        raise SecretStoreError(
            f"unknown secrets provider {provider!r}; use one of {PROVIDERS}"
        )
    return KeychainStore(stack)
