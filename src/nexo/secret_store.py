"""Where Nexo's generated secrets live: a provider, never a file you copy between machines.

    keychain  (default) the OS keychain through keyring: macOS Keychain, Linux Secret Service.
              One entry per key under "nexo/<stack>", so every checkout of a stack agrees.
              Where no keychain is set up, ~/.config/nexo/config: one [stack] section of
              default credentials for this machine, readable only by you.
    env       the environment: Kubernetes Secrets, External Secrets Operator, CI, `op run`,
              Vault Agent... Nexo reads them and never writes them.
    sops      a SOPS-encrypted dotenv file (age, PGP or cloud KMS keys), opt-in.
    vault     HashiCorp Vault or OpenBao, KV version 2, opt-in.

`nexo setup` renders the choice into .nexo/render/nexo.env (NEXO_SECRETS_*), and both
`nexo setup` and Nexo itself build the same store from it. Values are never logged.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from pathlib import Path

import keyring
import keyring.backends.fail
import keyring.errors

PROVIDERS = ("keychain", "env", "sops", "vault")


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


class ConfigFileStore(SecretStore):
    """~/.config/nexo/config: a [stack] section per stack, like ~/.aws/credentials.

    Default credentials for one machine when no keychain is set up. Only its owner can read
    it (directory 0700, file 0600); it is never meant to leave the machine.
    """

    def __init__(self, stack: str):
        self.path = config_dir() / "config"
        self.stack = stack
        self.label = f"{self.path} [{stack}]"

    def _read(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str  # keep NEO4J_PASSWORD as written
        if self.path.exists():
            parser.read(self.path, encoding="utf-8")
        return parser

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        parser = self._read()
        if not parser.has_section(self.stack):
            return {}
        section = parser[self.stack]
        return {key: section[key] for key in keys if section.get(key)}

    def put(self, values: Mapping[str, str]) -> None:
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


class KeychainStore(SecretStore):
    """The OS keychain, one entry per key under the service "nexo/<stack>"."""

    def __init__(self, stack: str):
        self.service = f"nexo/{stack}"
        self.label = f"keychain ({self.service})"

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        try:
            found = {key: keyring.get_password(self.service, key) for key in keys}
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(f"{self.label}: {type(exc).__name__}") from exc
        return {key: value for key, value in found.items() if value}

    def put(self, values: Mapping[str, str]) -> None:
        try:
            for key, value in values.items():
                keyring.set_password(self.service, key, value)
        except keyring.errors.KeyringError as exc:
            raise SecretStoreError(f"{self.label}: {type(exc).__name__}") from exc


class EnvStore(SecretStore):
    """Values injected by whatever manages secrets (Kubernetes, CI, op run); read-only."""

    label = "the environment"
    writable = False

    def get(self, keys: Iterable[str]) -> dict[str, str]:
        return {key: os.environ[key] for key in keys if os.environ.get(key)}

    def put(self, values: Mapping[str, str]) -> None:
        raise SecretStoreError(
            "provider env is read-only: provide "
            + ", ".join(sorted(values))
            + " through your secret manager or environment"
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
    provider = options.get("NEXO_SECRETS_PROVIDER") or "keychain"
    if provider == "env":
        return EnvStore()
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
    if isinstance(keyring.get_keyring(), keyring.backends.fail.Keyring):
        return ConfigFileStore(stack)  # no keychain set up here
    return KeychainStore(stack)
