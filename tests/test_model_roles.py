"""Per-role model endpoints: each role can live on its own host and model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from nexo.config import Settings
from nexo.llm import build_chat_model, build_strands_model


def test_roles_fall_back_to_the_default_endpoint_field_by_field():
    settings = Settings(_env_file=None, vllm_base_url="http://default:8000/v1", vllm_model="default-model",
                        vllm_api_key=SecretStr("default-key"),
                        agent_base_url="http://m1-max.local:11434/v1", agent_model="big-agent-model",
                        router_model="small-router-model")
    agent, router, extraction = (settings.endpoint(r) for r in ("agent", "router", "extraction"))
    assert (agent.base_url, agent.model) == ("http://m1-max.local:11434/v1", "big-agent-model")
    assert agent.api_key.get_secret_value() == "default-key"  # not overridden -> default
    assert (router.base_url, router.model) == ("http://default:8000/v1", "small-router-model")
    assert (extraction.base_url, extraction.model) == ("http://default:8000/v1", "default-model")


def test_role_overrides_come_from_environment(monkeypatch):
    monkeypatch.setenv("EXTRACTION_BASE_URL", "http://m5-pro.local:11434/v1")
    monkeypatch.setenv("EXTRACTION_MODEL", "extractor")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    settings = Settings(_env_file=None)
    assert settings.endpoint("extraction").base_url == "http://m5-pro.local:11434/v1"
    assert settings.endpoint("extraction").model == "extractor"
    assert settings.endpoint("judge").api_key.get_secret_value() == "judge-key"
    assert settings.endpoint("agent").model == settings.vllm_model


ROOT = Path(__file__).resolve().parent.parent
PASSTHROUGH = {"VLLM_IMAGE_TAG", "HF_TOKEN", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT"}  # read by compose/collector


def _env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text().splitlines():
        line = line.strip().lstrip("#").strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line):
            keys.add(line.split("=", 1)[0])
    return keys


@pytest.mark.parametrize("template", [".env.example", "deploy/local/local.env.example"])
def test_env_templates_only_use_real_settings(template):
    known = {name.upper() for name in Settings.model_fields}
    unknown = _env_keys(ROOT / template) - known - PASSTHROUGH
    assert not unknown, f"{template} sets keys Nexo never reads: {sorted(unknown)}"


def test_reasoning_effort_is_sent_only_when_set_and_never_to_the_agent():
    plain = build_chat_model(Settings(_env_file=None), role="extraction")
    assert plain.reasoning_effort is None
    assert "reasoning_effort" not in plain._default_params
    settings = Settings(_env_file=None, extraction_reasoning_effort="none", judge_reasoning_effort="low")
    assert build_chat_model(settings, role="extraction")._default_params["reasoning_effort"] == "none"
    assert build_chat_model(settings, role="judge")._default_params["reasoning_effort"] == "low"
    assert "reasoning_effort" not in build_chat_model(settings, role="router")._default_params  # per role
    assert settings.endpoint("agent").reasoning_effort is None
    assert "reasoning_effort" not in (build_strands_model(settings).config.get("params") or {})


def test_clients_are_built_from_their_role():
    settings = Settings(_env_file=None, vllm_model="default-model",
                        agent_base_url="http://agent-host:11434/v1", agent_model="agent-model",
                        router_base_url="http://router-host:11434/v1", router_model="router-model")
    router = build_chat_model(settings, role="router")
    assert str(router.openai_api_base) == "http://router-host:11434/v1" and router.model_name == "router-model"
    extraction = build_chat_model(settings, role="extraction")
    assert extraction.model_name == "default-model"
    agent = build_strands_model(settings)
    assert agent.config["model_id"] == "agent-model"
    assert agent.client_args["base_url"] == "http://agent-host:11434/v1"
