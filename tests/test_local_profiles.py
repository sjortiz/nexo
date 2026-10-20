"""Hardware profiles: catalog, settings resolution, setup script and docs stay consistent."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexo import local_profiles as lp
from nexo.config import Settings
from nexo.setup.render import build_env
from nexo.setup.schema import load_casting

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "local" / "ollama-setup.sh"


# --- catalog ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "chip", "tier"),
    [
        ("m1-max-64", "M1 Max", 64),
        ("m5-pro-24", "M5 Pro", 24),
        ("m1-pro-16", "M1 Pro", 16),
        ("m3-36", "M3", 32),
        ("m2-ultra-192", "M2 Ultra", 64),
        ("M1-Max-48", "M1 Max", 48),
    ],
)
def test_profile_names_pick_the_largest_tier_that_fits(name, chip, tier):
    """Mac profile names parse to their chip and the largest memory tier that fits."""
    host = lp.parse_profile(name)
    assert (host.chip, host.tier_gb) == (chip, tier)


@pytest.mark.parametrize(
    ("name", "kind", "tier"),
    [
        ("gpu-24", "gpu", 24),
        ("GPU-80", "gpu", 64),
        ("gpu-16", "gpu", 16),
        ("cpu-64", "cpu", 64),
        ("cpu-20", "cpu", 16),
    ],
)
def test_linux_gpu_and_cpu_profiles_pick_tiers_by_vram_or_ram(name, kind, tier):
    """gpu-/cpu- profiles pick their tier from VRAM or RAM and use the kind as chip."""
    host = lp.parse_profile(name)
    assert (host.kind, host.tier_gb, host.chip) == (kind, tier, kind.upper())


@pytest.mark.parametrize(
    "name",
    [
        "m1-max",
        "x1-max-64",
        "m1-mega-64",
        "m1-max-64gb",
        "",
        "m1-max-8",
        "gpu",
        "gpu-8",
        "tpu-64",
        "gpu-24-max",
        "cpu--64",
        "m9-ultra-999",  # no such chip or memory size
        "m6-max-64",
        "m4-max-100",
    ],
)
def test_invalid_or_too_small_profiles_are_rejected(name):
    """Malformed, unknown or under-16 GB profile names raise ProfileError."""
    with pytest.raises(lp.ProfileError):
        lp.parse_profile(name)


def test_too_small_errors_name_the_right_kind_of_memory():
    """Too-small errors mention unified memory, VRAM or RAM according to the profile kind."""
    for name, unit in (("m1-8", "unified memory"), ("gpu-8", "VRAM"), ("cpu-8", "RAM")):
        with pytest.raises(lp.ProfileError, match=unit):
            lp.parse_profile(name)


def test_a_mac_can_use_a_linux_gpu_box_for_the_agent():
    """A remote gpu profile serves the agent while the router stays on the local Mac."""
    settings = Settings(
        _env_file=None,
        local_profile="m5-pro-24",
        remote_profile="gpu-48",
        remote_base_url="http://localhost:11436/v1",
    )
    assert settings.endpoint("agent").model == lp.plan("gpu-48", "heavy").roles["agent"]
    assert settings.endpoint("agent").base_url == "http://localhost:11436/v1"
    assert settings.endpoint("router").base_url == "http://localhost:11434/v1"


@pytest.mark.parametrize("tier", sorted(lp.TIERS))
@pytest.mark.parametrize("role_set", sorted(lp.ROLE_SETS))
def test_every_tier_fits_its_memory_and_covers_its_roles(tier, role_set):
    """Each tier/role-set plan covers its roles, uses catalogued models and fits in 70% of RAM."""
    result = lp.plan(f"m1-{tier}", role_set)
    assert set(result.roles) == set(lp.ROLE_SETS[role_set])
    assert all(model in lp.MODEL_SIZES_GB for model in result.models)
    # Leave ~30% for macOS, KV cache and (on the Nexo Mac) Neo4j.
    assert (
        result.weights_gb <= tier * 0.7
    ), f"{result.models} = {result.weights_gb} GB on a {tier} GB Mac"
    if role_set != "heavy":
        assert result.roles[
            "embedding"
        ], "every Mac that runs Nexo needs an embedding model"


def test_heavy_and_light_split_the_single_role_set():
    """The heavy and light role sets partition the single role set without overlap."""
    assert set(lp.ROLE_SETS["heavy"]) | set(lp.ROLE_SETS["light"]) == set(
        lp.ROLE_SETS["single"]
    )
    assert not set(lp.ROLE_SETS["heavy"]) & set(lp.ROLE_SETS["light"])


def test_cli_prints_a_shell_plan_and_rejects_bad_profiles(capsys):
    """The module CLI prints shell variables for a plan and exits 2 on a bad profile."""
    # The module CLI that ollama-setup.sh runs has no public alias.
    cli = lp._main  # pylint: disable=protected-access
    assert cli(["plan", "m5-pro-24", "light", "--format", "shell"]) == 0
    out = capsys.readouterr().out
    assert (
        "MODELS='bge-m3 llama3.1:8b-instruct-q4_K_M qwen3:4b-instruct-2507-q4_K_M'"
        in out
    )
    assert "EMBEDDING_MODELS='bge-m3'" in out
    assert cli(["plan", "m1-max-8"]) == 2


# --- settings ---------------------------------------------------------------------


def test_without_profiles_settings_are_untouched():
    """Without profiles, Settings keep the vLLM service endpoint and default embedding model."""
    settings = Settings(_env_file=None)
    assert (
        settings.endpoint("agent").base_url == "http://vllm-inference-service:8000/v1"
    )
    assert settings.embedding_model == "BAAI/bge-m3"


def test_single_mac_profile_serves_every_role_locally():
    """One Mac profile points every role at local Ollama with per-role reasoning effort."""
    settings = Settings(_env_file=None, local_profile="m1-max-64")
    endpoints = {
        r: settings.endpoint(r) for r in ("agent", "extraction", "router", "judge")
    }
    assert {e.base_url for e in endpoints.values()} == {"http://localhost:11434/v1"}
    assert (
        endpoints["agent"].model
        == endpoints["extraction"].model
        == "qwen3.6:35b-a3b-q4_K_M"
    )
    assert (
        endpoints["extraction"].reasoning_effort == "none"
    )  # thinking model, JSON role
    assert endpoints["agent"].reasoning_effort is None  # the agent keeps its reasoning
    assert (
        endpoints["router"].reasoning_effort is None
    )  # non-thinking model: parameter not sent
    assert (settings.embedding_model, settings.embedding_dimensions) == ("bge-m3", 1024)
    assert settings.embedding_base_url == "http://localhost:11434/v1"


def test_reasoning_follows_an_explicitly_set_model():
    """An explicit model decides reasoning for its JSON role, not the profile's model."""
    settings = Settings(
        _env_file=None, local_profile="m1-max-32", extraction_model="gemma4:31b"
    )
    assert settings.endpoint("extraction").model == "gemma4:31b"
    assert settings.endpoint("extraction").reasoning_effort == "none"
    non_thinking = Settings(
        _env_file=None,
        local_profile="m1-max-64",
        extraction_model="qwen3:30b-a3b-instruct-2507-q4_K_M",
    )
    assert non_thinking.endpoint("extraction").reasoning_effort is None


def test_small_single_mac_disables_the_judge():
    """A 16 GB Mac profile disables the judge and shares one model for router and agent."""
    settings = Settings(_env_file=None, local_profile="m1-pro-16")
    assert settings.judge_model is None
    assert (
        settings.endpoint("router").model
        == settings.endpoint("agent").model
        == "qwen3.5:9b-q4_K_M"
    )


def test_a_judge_set_on_a_small_mac_runs_on_the_local_server():
    """Naming a judge model enables it on the profile's Ollama, not the vLLM default."""
    judge = "llama3.1:8b-instruct-q4_K_M"
    settings = Settings(_env_file=None, local_profile="m5-pro-24", judge_model=judge)
    endpoint = settings.endpoint("judge")
    assert (endpoint.model, endpoint.base_url) == (judge, settings.local_base_url)
    assert endpoint.api_key.get_secret_value() == "ollama"
    explicit = Settings(
        _env_file=None,
        local_profile="m5-pro-24",
        judge_model=judge,
        judge_base_url="http://judge.local/v1",
    )
    assert explicit.endpoint("judge").base_url == "http://judge.local/v1"
    assert Settings(_env_file=None, local_profile="m5-pro-24").judge_model is None


def test_two_mac_profiles_split_roles_between_hosts():
    """With two Macs, agent/extraction go remote and router/judge/embeddings stay local."""
    settings = Settings(
        _env_file=None, local_profile="m5-pro-24", remote_profile="m1-max-32"
    )
    agent, extraction, router, judge = (
        settings.endpoint(r) for r in ("agent", "extraction", "router", "judge")
    )
    assert agent.base_url == extraction.base_url == "http://localhost:11435/v1"
    assert agent.model == "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert (
        router.base_url
        == judge.base_url
        == settings.embedding_base_url
        == "http://localhost:11434/v1"
    )
    assert (router.model, judge.model) == (
        "qwen3:4b-instruct-2507-q4_K_M",
        "llama3.1:8b-instruct-q4_K_M",
    )
    assert (
        judge.model.split(":")[0] != agent.model.split(":")[0]
    )  # judge from another family


def test_explicit_settings_win_over_profiles(monkeypatch):
    """Explicit environment settings override the values a profile would supply."""
    monkeypatch.setenv("LOCAL_PROFILE", "m5-pro-24")
    monkeypatch.setenv("REMOTE_PROFILE", "m1-max-64")
    monkeypatch.setenv("AGENT_MODEL", "my-agent")
    monkeypatch.setenv("REMOTE_BASE_URL", "http://host.containers.internal:11435/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.setenv("EXTRACTION_REASONING_EFFORT", "low")
    settings = Settings(_env_file=None)
    assert settings.endpoint("agent").model == "my-agent"
    assert (
        settings.endpoint("agent").base_url
        == "http://host.containers.internal:11435/v1"
    )
    assert settings.endpoint("extraction").reasoning_effort == "low"
    assert settings.embedding_model == "qwen3-embedding:0.6b"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"remote_profile": "m1-max-64"},
        {"local_profile": "m1-max-8"},
        {"local_profile": "pc-64"},
    ],
)
def test_bad_profile_settings_fail_fast(kwargs):
    """Settings reject a remote profile without a local one and invalid profile names."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


# --- templates, script and docs ------------------------------------------------------


def test_template_and_mac_preset_resolve_to_a_single_mac(tmp_path):
    """The env template plus what the mac-single preset renders give one Mac on local Ollama."""
    casting = load_casting(ROOT / "examples/setups/mac-single/nexo.yaml")
    rendered = tmp_path / "nexo.env"
    rendered.write_text("".join(f"{k}={v}\n" for k, v in build_env(casting).items()))
    settings = Settings(
        _env_file=(ROOT / "deploy" / "local" / "local.env.example", rendered)
    )
    assert settings.local_profile == "m5-pro-24" and settings.remote_profile is None
    assert settings.endpoint("agent").base_url == "http://localhost:11434/v1"


def _run_script(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    # PATH with this interpreter's python3 but without Ollama: the script must resolve
    # the plan, then stop before touching Ollama (macOS: Homebrew; Linux: /usr/local/bin).
    path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    if shutil.which("ollama", path=path):
        pytest.skip("ollama is on the test PATH; cannot check the not-installed path")
    # NEXO_ENV_FILE: never read the developer's own .env from the repository.
    clean = {
        "PATH": path,
        "HOME": os.environ.get("HOME", "/tmp"),
        "NEXO_ENV_FILE": os.devnull,
        **env,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=clean,
        cwd=ROOT,
        timeout=60,
        check=False,
    )


def test_setup_script_pulls_exactly_the_catalog_models():
    """ollama-setup.sh announces exactly the models the catalog plans for each role set."""
    result = _run_script(LOCAL_PROFILE="m5-pro-24", REMOTE_PROFILE="m1-max-64")
    assert (
        "role set light: bge-m3 llama3.1:8b-instruct-q4_K_M qwen3:4b-instruct-2507-q4_K_M"
        in result.stdout
    )
    heavy = _run_script("m1-max-64", "heavy")
    assert (
        f"role set heavy: {' '.join(lp.plan('m1-max-64', 'heavy').models)}"
        in heavy.stdout
    )


def test_setup_script_reads_profiles_from_the_env_file(tmp_path):
    """ollama-setup.sh takes its profiles from the file named by NEXO_ENV_FILE."""
    env_file = tmp_path / ".env"
    env_file.write_text("LOCAL_PROFILE=m5-pro-24\nREMOTE_PROFILE=m1-max-64\n")
    result = _run_script(NEXO_ENV_FILE=str(env_file))
    assert "profile m5-pro-24, role set light:" in result.stdout


def test_setup_script_fails_clearly_without_a_profile_or_with_a_bad_one():
    """ollama-setup.sh exits 2 with usage when no profile is set and fails on a too-small one."""
    missing = _run_script(LOCAL_PROFILE="", REMOTE_PROFILE="")
    assert missing.returncode == 2 and "usage:" in missing.stderr
    bad = _run_script("m1-max-8")
    assert bad.returncode != 0 and "at least 16 GB" in bad.stderr


class _FakeOllamaAPI(BaseHTTPRequestHandler):
    """Fake Ollama HTTP API that records each request's method, path and model."""

    calls: list[tuple[str, str, str]] = []

    def _reply(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        model = json.loads(body).get("model", "") if body else ""
        self.calls.append((self.command, self.path, model))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    do_GET = do_POST = _reply

    def log_message(self, *args) -> None:  # keep test output clean
        pass


def test_setup_script_pulls_and_preloads_through_an_already_running_server(tmp_path):
    """Against a running Ollama, the script pulls and preloads each model without restarting it."""
    if not shutil.which("curl"):
        pytest.skip("curl not installed")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaAPI)
    _FakeOllamaAPI.calls = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "ollama.log"
    (fake_bin / "ollama").write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
    (fake_bin / "ollama").chmod(0o755)
    path = os.pathsep.join(
        [
            str(fake_bin),
            str(Path(sys.executable).parent),
            os.path.dirname(shutil.which("curl")),
            "/usr/bin",
            "/bin",
        ]
    )
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "m5-pro-24", "light"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=60,
            env={
                "PATH": path,
                "HOME": str(tmp_path),
                "OLLAMA_HOST": f"127.0.0.1:{server.server_port}",
            },
            check=False,
        )
    finally:
        server.shutdown()
    assert result.returncode == 0, result.stderr
    assert (
        "started ollama serve" not in result.stdout
    )  # the running service is left alone
    assert (
        "WARNING: ollama is already running" in result.stderr
        and "were NOT applied" in result.stderr
        and "OLLAMA_CONTEXT_LENGTH=16384" in result.stderr
    )
    if sys.platform == "darwin":
        assert "--apply" in result.stderr
    else:
        assert "ollama.service.d/nexo.conf" in result.stderr
        assert 'Environment="OLLAMA_CONTEXT_LENGTH=16384"' in result.stderr
    expected = lp.plan("m5-pro-24", "light").models
    # Not a terminal: pulls go through the API with plain progress lines, not the CLI.
    assert log.read_text().splitlines() == ["ps"]
    pulls = [m for method, path, m in _FakeOllamaAPI.calls if path == "/api/pull"]
    assert pulls == expected
    assert all(f"pull {m}: started" in result.stdout for m in expected)
    preloads = {
        (path, model)
        for method, path, model in _FakeOllamaAPI.calls
        if method == "POST" and path != "/api/pull"
    }
    assert preloads == {
        ("/api/embed", "bge-m3"),
        ("/api/generate", "llama3.1:8b-instruct-q4_K_M"),
        ("/api/generate", "qwen3:4b-instruct-2507-q4_K_M"),
    }


def test_docs_list_every_catalog_model_and_tier():
    """The Apple Silicon playbook lists every catalog tier and model."""
    page = (ROOT / "docs" / "operations" / "local-apple-silicon.md").read_text()
    for tier in lp.TIERS:
        assert re.search(
            rf"\|\s*{tier} GB", page
        ), f"tier {tier} GB missing from the playbook"
    for model in lp.MODEL_SIZES_GB:
        assert f"`{model}`" in page, f"{model} missing from the playbook"
