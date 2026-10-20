"""Secret providers: keychain (default), the per-user fallback, env, sops and vault."""

from __future__ import annotations

import base64
import io
import json
import stat
import subprocess
import urllib.error
from pathlib import Path

import keyring
import keyring.backends.fail
import pytest

from nexo import secret_store
from nexo.config import Settings
from nexo.secret_store import (
    EnvStore,
    KeychainStore,
    SecretStoreError,
    SopsStore,
    ConfigFileStore,
    VaultStore,
    store_from_options,
)


def test_keychain_entries_are_per_stack(memory_keychain):
    """Every checkout of a stack shares its secrets; another stack (metadata.name) does not."""
    nexo = store_from_options({"NEXO_STACK": "nexo"})
    assert isinstance(nexo, KeychainStore)
    nexo.put({"NEO4J_PASSWORD": "one"})
    assert store_from_options({"NEXO_STACK": "nexo"}).get(["NEO4J_PASSWORD"]) == {
        "NEO4J_PASSWORD": "one"
    }
    assert not store_from_options({"NEXO_STACK": "nexoask"}).get(["NEO4J_PASSWORD"])
    assert ("nexo/nexo", "NEO4J_PASSWORD") in memory_keychain.items


def test_without_a_keychain_credentials_go_to_the_private_config_file():
    """No keychain set up: ~/.config/nexo/config, a [stack] section each, owner-only."""
    keyring.set_keyring(keyring.backends.fail.Keyring())
    store = store_from_options({"NEXO_STACK": "nexo"})
    assert isinstance(store, ConfigFileStore)
    store.put({"NEO4J_PASSWORD": "1"})
    store.put({"SIGNOZ_JWT_SECRET": "2"})
    other = store_from_options({"NEXO_STACK": "nexoask"})
    other.put({"NEO4J_PASSWORD": "3"})
    assert store.get(["NEO4J_PASSWORD", "SIGNOZ_JWT_SECRET", "C"]) == {
        "NEO4J_PASSWORD": "1",
        "SIGNOZ_JWT_SECRET": "2",
    }
    assert other.get(["NEO4J_PASSWORD"]) == {"NEO4J_PASSWORD": "3"}
    assert store.path.name == "config" and store.path == other.path
    assert "[nexo]" in store.path.read_text() and "[nexoask]" in store.path.read_text()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_env_provider_reads_and_never_writes(monkeypatch):
    """Opting out: values come from the environment (Kubernetes, CI); Nexo writes nothing."""
    monkeypatch.setenv("NEO4J_PASSWORD", "from-k8s")
    store = store_from_options({"NEXO_SECRETS_PROVIDER": "env"})
    assert isinstance(store, EnvStore) and not store.writable
    assert store.get(["NEO4J_PASSWORD", "MISSING"]) == {"NEO4J_PASSWORD": "from-k8s"}
    with pytest.raises(SecretStoreError, match="MISSING"):
        store.put({"MISSING": "x"})


def _fake_sops(monkeypatch, fail_with: str | None = None):
    """sops that 'encrypts' by base64 and needs a creation rule unless fail_with is None."""

    def run(command, **kwargs):
        if fail_with:
            return subprocess.CompletedProcess(command, 1, "", f"sops: {fail_with}\n")
        if "--encrypt" in command:
            text = base64.b64encode(kwargs["input"].encode()).decode()
            return subprocess.CompletedProcess(command, 0, f"ENC={text}\n", "")
        stored = Path(command[-1]).read_text(encoding="utf-8").split("=", 1)[1]
        return subprocess.CompletedProcess(
            command, 0, base64.b64decode(stored).decode(), ""
        )

    monkeypatch.setattr(secret_store.subprocess, "run", run)


def test_sops_keeps_only_ciphertext_on_disk(tmp_path, monkeypatch):
    """Opt-in SOPS: values round-trip, merge with earlier ones, never land on disk in clear."""
    _fake_sops(monkeypatch)
    path = tmp_path / "secrets" / "nexo.sops.env"
    store = SopsStore(path)
    assert not store.get(["A"])  # no file yet
    store.put({"A": "alpha"})
    store.put({"B": "beta"})
    assert store.get(["A", "B"]) == {"A": "alpha", "B": "beta"}
    assert "alpha" not in path.read_text() and "beta" not in path.read_text()


def test_sops_errors_explain_without_values(tmp_path, monkeypatch):
    """A missing creation rule or sops binary is a clear SecretStoreError."""
    _fake_sops(
        monkeypatch, fail_with="error loading config: no matching creation rules found"
    )
    with pytest.raises(SecretStoreError, match="no matching creation rules"):
        SopsStore(tmp_path / "x.env").put({"A": "secret-value"})

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("sops")

    monkeypatch.setattr(secret_store.subprocess, "run", missing)
    with pytest.raises(SecretStoreError, match="sops is not installed"):
        SopsStore(tmp_path / "x.env").put({"A": "secret-value"})


def _fake_vault(monkeypatch, status: int | None = None):
    """A KV v2 endpoint in memory; status forces an HTTP error."""
    data: dict[str, str] = {}

    def urlopen(
        request, timeout
    ):  # pylint: disable=unused-argument  # urlopen's signature
        if status:
            raise urllib.error.HTTPError(request.full_url, status, "no", {}, None)
        assert request.get_header("X-vault-token") == "t0ken"
        if request.get_method() == "POST":
            data.clear()
            data.update(json.loads(request.data)["data"])
            return io.BytesIO(b"{}")
        if not data:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return io.BytesIO(json.dumps({"data": {"data": data}}).encode())

    monkeypatch.setattr(secret_store.urllib.request, "urlopen", urlopen)
    return data


def test_vault_kv2_round_trip_merges(monkeypatch):
    """Opt-in Vault/OpenBao: one KV secret per stack; writes keep the other keys."""
    monkeypatch.setenv("VAULT_TOKEN", "t0ken")
    data = _fake_vault(monkeypatch)
    store = store_from_options(
        {"NEXO_SECRETS_PROVIDER": "vault", "NEXO_SECRETS_VAULT_ADDR": "http://v:8200"}
    )
    assert isinstance(store, VaultStore) and store.url.endswith(
        "/v1/secret/data/nexo/nexo"
    )
    assert not store.get(["A"])
    store.put({"A": "1"})
    store.put({"B": "2"})
    assert data == {"A": "1", "B": "2"}


def test_vault_errors_name_the_problem_not_the_token(monkeypatch):
    """No token, or a refused one, is a SecretStoreError that never shows the token."""
    store = VaultStore("http://v:8200", "secret", "nexo/nexo", "VAULT_TOKEN")
    with pytest.raises(SecretStoreError, match="set VAULT_TOKEN"):
        store.get(["A"])
    monkeypatch.setenv("VAULT_TOKEN", "t0ken")
    _fake_vault(monkeypatch, status=403)
    with pytest.raises(SecretStoreError, match="HTTP 403") as raised:
        store.get(["A"])
    assert "t0ken" not in str(raised.value)
    with pytest.raises(SecretStoreError, match="VAULT_ADDR"):
        store_from_options({"NEXO_SECRETS_PROVIDER": "vault"})


def test_settings_take_generated_secrets_from_the_provider(tmp_path, monkeypatch):
    """The keychain wins over a stale .env; the environment wins over both; tools opt out."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("NEO4J_PASSWORD=stale\n")
    assert Settings().neo4j_password.get_secret_value() == "stale"  # no nexo setup yet
    (tmp_path / ".nexo/render").mkdir(parents=True)
    (tmp_path / ".nexo/render/nexo.env").write_text(
        "NEXO_STACK=nexo\nNEXO_SECRETS_PROVIDER=keychain\n"
    )
    KeychainStore("nexo").put({"NEO4J_PASSWORD": "from-keychain"})
    assert Settings().neo4j_password.get_secret_value() == "from-keychain"
    assert Settings(_env_file=None).neo4j_password.get_secret_value() == ""
    monkeypatch.setenv("NEO4J_PASSWORD", "from-env")
    assert Settings().neo4j_password.get_secret_value() == "from-env"
