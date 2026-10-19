"""Hardware profiles: catalog, settings resolution, setup script and docs stay consistent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from nexo import local_profiles as lp
from nexo.config import Settings

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "local" / "ollama-setup.sh"


# --- catalog ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("name", "chip", "tier"),
    [("m1-max-64", "M1 Max", 64), ("m5-pro-24", "M5 Pro", 24), ("m1-pro-16", "M1 Pro", 16),
     ("m3-36", "M3", 32), ("m2-ultra-192", "M2 Ultra", 64), ("M1-Max-48", "M1 Max", 48)],
)
def test_profile_names_pick_the_largest_tier_that_fits(name, chip, tier):
    host = lp.parse_profile(name)
    assert (host.chip, host.tier_gb) == (chip, tier)


@pytest.mark.parametrize(
    ("name", "kind", "tier"),
    [("gpu-24", "gpu", 24), ("GPU-80", "gpu", 64), ("gpu-16", "gpu", 16), ("cpu-64", "cpu", 64), ("cpu-20", "cpu", 16)],
)
def test_linux_gpu_and_cpu_profiles_pick_tiers_by_vram_or_ram(name, kind, tier):
    host = lp.parse_profile(name)
    assert (host.kind, host.tier_gb, host.chip) == (kind, tier, kind.upper())


@pytest.mark.parametrize("name", ["m1-max", "x1-max-64", "m1-mega-64", "m1-max-64gb", "", "m1-max-8",
                                  "gpu", "gpu-8", "tpu-64", "gpu-24-max", "cpu--64"])
def test_invalid_or_too_small_profiles_are_rejected(name):
    with pytest.raises(lp.ProfileError):
        lp.parse_profile(name)


def test_too_small_errors_name_the_right_kind_of_memory():
    for name, unit in (("m1-8", "unified memory"), ("gpu-8", "VRAM"), ("cpu-8", "RAM")):
        with pytest.raises(lp.ProfileError, match=unit):
            lp.parse_profile(name)


def test_a_mac_can_use_a_linux_gpu_box_for_the_agent():
    settings = Settings(_env_file=None, local_profile="m5-pro-24", remote_profile="gpu-48",
                        remote_base_url="http://localhost:11436/v1")
    assert settings.endpoint("agent").model == lp.plan("gpu-48", "heavy").roles["agent"]
    assert settings.endpoint("agent").base_url == "http://localhost:11436/v1"
    assert settings.endpoint("router").base_url == "http://localhost:11434/v1"


@pytest.mark.parametrize("tier", sorted(lp.TIERS))
@pytest.mark.parametrize("role_set", sorted(lp.ROLE_SETS))
def test_every_tier_fits_its_memory_and_covers_its_roles(tier, role_set):
    result = lp.plan(f"m1-{tier}", role_set)
    assert set(result.roles) == set(lp.ROLE_SETS[role_set])
    assert all(model in lp.MODEL_SIZES_GB for model in result.models)
    # Leave ~30% for macOS, KV cache and (on the Nexo Mac) Neo4j.
    assert result.weights_gb <= tier * 0.7, f"{result.models} = {result.weights_gb} GB on a {tier} GB Mac"
    if role_set != "heavy":
        assert result.roles["embedding"], "every Mac that runs Nexo needs an embedding model"


def test_heavy_and_light_split_the_single_role_set():
    assert set(lp.ROLE_SETS["heavy"]) | set(lp.ROLE_SETS["light"]) == set(lp.ROLE_SETS["single"])
    assert not set(lp.ROLE_SETS["heavy"]) & set(lp.ROLE_SETS["light"])


def test_cli_prints_a_shell_plan_and_rejects_bad_profiles(capsys):
    assert lp._main(["plan", "m5-pro-24", "light", "--format", "shell"]) == 0
    out = capsys.readouterr().out
    assert "MODELS='bge-m3 llama3.1:8b qwen3:4b-instruct-2507'" in out and "EMBEDDING_MODELS='bge-m3'" in out
    assert lp._main(["plan", "m1-max-8"]) == 2


# --- settings ---------------------------------------------------------------------

def test_without_profiles_settings_are_untouched():
    settings = Settings(_env_file=None)
    assert settings.endpoint("agent").base_url == "http://vllm-inference-service:8000/v1"
    assert settings.embedding_model == "BAAI/bge-m3"


def test_single_mac_profile_serves_every_role_locally():
    settings = Settings(_env_file=None, local_profile="m1-max-64")
    endpoints = {r: settings.endpoint(r) for r in ("agent", "extraction", "router", "judge")}
    assert {e.base_url for e in endpoints.values()} == {"http://localhost:11434/v1"}
    assert endpoints["agent"].model == endpoints["extraction"].model == "qwen3.6:35b-a3b-q4_K_M"
    assert endpoints["extraction"].reasoning_effort == "none"  # thinking model, JSON role
    assert endpoints["agent"].reasoning_effort is None  # the agent keeps its reasoning
    assert endpoints["router"].reasoning_effort is None  # non-thinking model: parameter not sent
    assert (settings.embedding_model, settings.embedding_dimensions) == ("bge-m3", 1024)
    assert settings.embedding_base_url == "http://localhost:11434/v1"


def test_small_single_mac_disables_the_judge():
    settings = Settings(_env_file=None, local_profile="m1-pro-16")
    assert settings.judge_model is None
    assert settings.endpoint("router").model == settings.endpoint("agent").model == "qwen3.5:9b"


def test_two_mac_profiles_split_roles_between_hosts():
    settings = Settings(_env_file=None, local_profile="m5-pro-24", remote_profile="m1-max-32")
    agent, extraction, router, judge = (settings.endpoint(r) for r in ("agent", "extraction", "router", "judge"))
    assert agent.base_url == extraction.base_url == "http://localhost:11435/v1"
    assert agent.model == "qwen3:30b-a3b-instruct-2507-q4_K_M"
    assert router.base_url == judge.base_url == settings.embedding_base_url == "http://localhost:11434/v1"
    assert (router.model, judge.model) == ("qwen3:4b-instruct-2507", "llama3.1:8b")
    assert judge.model.split(":")[0] != agent.model.split(":")[0]  # judge from another family


def test_explicit_settings_win_over_profiles(monkeypatch):
    monkeypatch.setenv("LOCAL_PROFILE", "m5-pro-24")
    monkeypatch.setenv("REMOTE_PROFILE", "m1-max-64")
    monkeypatch.setenv("AGENT_MODEL", "my-agent")
    monkeypatch.setenv("REMOTE_BASE_URL", "http://host.containers.internal:11435/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.setenv("EXTRACTION_REASONING_EFFORT", "low")
    settings = Settings(_env_file=None)
    assert settings.endpoint("agent").model == "my-agent"
    assert settings.endpoint("agent").base_url == "http://host.containers.internal:11435/v1"
    assert settings.endpoint("extraction").reasoning_effort == "low"
    assert settings.embedding_model == "qwen3-embedding:0.6b"


@pytest.mark.parametrize("kwargs", [{"remote_profile": "m1-max-64"}, {"local_profile": "m1-max-8"},
                                    {"local_profile": "pc-64"}])
def test_bad_profile_settings_fail_fast(kwargs):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)


# --- templates, script and docs ------------------------------------------------------

def test_local_env_template_resolves_to_a_single_mac():
    settings = Settings(_env_file=ROOT / "deploy" / "local" / "local.env.example")
    assert settings.local_profile == "m1-max-64" and settings.remote_profile is None
    assert settings.endpoint("agent").base_url == "http://localhost:11434/v1"


def _run_script(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    # PATH with this interpreter's python3 but without Ollama: the script must resolve the
    # plan, then stop before touching Ollama (macOS: Homebrew; Linux: /usr/local/bin).
    path = os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"])
    if shutil.which("ollama", path=path):
        pytest.skip("ollama is on the test PATH; cannot check the not-installed path")
    clean = {"PATH": path, "HOME": os.environ.get("HOME", "/tmp"), **env}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=clean, cwd=ROOT,
                          timeout=60)


def test_setup_script_pulls_exactly_the_catalog_models():
    result = _run_script(LOCAL_PROFILE="m5-pro-24", REMOTE_PROFILE="m1-max-64")
    assert "role set light: bge-m3 llama3.1:8b qwen3:4b-instruct-2507" in result.stdout
    heavy = _run_script("m1-max-64", "heavy")
    assert f"role set heavy: {' '.join(lp.plan('m1-max-64', 'heavy').models)}" in heavy.stdout


def test_setup_script_fails_clearly_without_a_profile_or_with_a_bad_one():
    missing = _run_script(LOCAL_PROFILE="", REMOTE_PROFILE="")
    assert missing.returncode == 2 and "usage:" in missing.stderr
    bad = _run_script("m1-max-8")
    assert bad.returncode != 0 and "at least 16 GB" in bad.stderr


def test_docs_list_every_catalog_model_and_tier():
    page = (ROOT / "docs" / "operations" / "local-apple-silicon.md").read_text()
    for tier in lp.TIERS:
        assert re.search(rf"\|\s*{tier} GB", page), f"tier {tier} GB missing from the playbook"
    for model in lp.MODEL_SIZES_GB:
        assert f"`{model}`" in page, f"{model} missing from the playbook"
