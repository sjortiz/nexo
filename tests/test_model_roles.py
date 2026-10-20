"""Per-role model endpoints: each role can live on its own host and model."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from nexo.config import Settings
from nexo.llm import build_chat_model, build_strands_model


def test_roles_fall_back_to_the_default_endpoint_field_by_field():
    """Unset role fields fall back to the default endpoint one field at a time."""
    settings = Settings(
        _env_file=None,
        vllm_base_url="http://default:8000/v1",
        vllm_model="default-model",
        vllm_api_key=SecretStr("default-key"),
        agent_base_url="http://m1-max.local:11434/v1",
        agent_model="big-agent-model",
        router_model="small-router-model",
    )
    agent, router, extraction = (
        settings.endpoint(r) for r in ("agent", "router", "extraction")
    )
    assert (agent.base_url, agent.model) == (
        "http://m1-max.local:11434/v1",
        "big-agent-model",
    )
    assert (
        agent.api_key.get_secret_value() == "default-key"
    )  # not overridden -> default
    assert (router.base_url, router.model) == (
        "http://default:8000/v1",
        "small-router-model",
    )
    assert (extraction.base_url, extraction.model) == (
        "http://default:8000/v1",
        "default-model",
    )


def test_role_overrides_come_from_environment(monkeypatch):
    """Role-prefixed environment variables override that role's endpoint fields."""
    monkeypatch.setenv("EXTRACTION_BASE_URL", "http://m5-pro.local:11434/v1")
    monkeypatch.setenv("EXTRACTION_MODEL", "extractor")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    settings = Settings(_env_file=None)
    assert settings.endpoint("extraction").base_url == "http://m5-pro.local:11434/v1"
    assert settings.endpoint("extraction").model == "extractor"
    assert settings.endpoint("judge").api_key.get_secret_value() == "judge-key"
    assert settings.endpoint("agent").model == settings.vllm_model


ROOT = Path(__file__).resolve().parent.parent
# Keys the compose stack reads (and passes to the collector or Langfuse), not Settings.
COMPOSE_FILES = [
    "docker-compose.yml",
    "docker-compose.gpu.yml",
    "deploy/langfuse/compose.yml",
]
PASSTHROUGH = {
    name
    for compose_file in COMPOSE_FILES
    for name in re.findall(r"\$\{(\w+)", (ROOT / compose_file).read_text())
}


def _env_keys(path: Path) -> set[str]:
    keys = set()
    for line in path.read_text().splitlines():
        line = line.strip().lstrip("#").strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line):
            keys.add(line.split("=", 1)[0])
    return keys


def _request_params(model) -> dict:
    """Return the default parameters the chat client sends with every request."""
    # The test is about the request payload; langchain exposes it only privately.
    return model._default_params  # pylint: disable=protected-access


@pytest.mark.parametrize("template", [".env.example", "deploy/local/local.env.example"])
def test_env_templates_only_use_real_settings(template):
    """Env templates only set keys that Settings or the compose stack actually read."""
    known = {name.upper() for name in Settings.model_fields.keys()}
    unknown = _env_keys(ROOT / template) - known - PASSTHROUGH
    assert not unknown, f"{template} sets keys Nexo never reads: {sorted(unknown)}"


def test_reasoning_effort_is_sent_only_when_set_and_never_to_the_agent():
    """reasoning_effort reaches a role's requests only when configured, never the agent's."""
    plain = build_chat_model(Settings(_env_file=None), role="extraction")
    assert plain.reasoning_effort is None
    assert "reasoning_effort" not in _request_params(plain)
    settings = Settings(
        _env_file=None, extraction_reasoning_effort="none", judge_reasoning_effort="low"
    )
    assert (
        _request_params(build_chat_model(settings, role="extraction"))[
            "reasoning_effort"
        ]
        == "none"
    )
    assert (
        _request_params(build_chat_model(settings, role="judge"))["reasoning_effort"]
        == "low"
    )
    assert "reasoning_effort" not in _request_params(
        build_chat_model(settings, role="router")
    )  # per role
    assert settings.endpoint("agent").reasoning_effort is None
    assert "reasoning_effort" not in (
        build_strands_model(settings).config.get("params") or {}
    )


def test_clients_are_built_from_their_role():
    """Chat and Strands clients use their own role's base URL and model."""
    settings = Settings(
        _env_file=None,
        vllm_model="default-model",
        agent_base_url="http://agent-host:11434/v1",
        agent_model="agent-model",
        router_base_url="http://router-host:11434/v1",
        router_model="router-model",
    )
    router = build_chat_model(settings, role="router")
    assert (
        str(router.openai_api_base) == "http://router-host:11434/v1"
        and router.model_name == "router-model"
    )
    extraction = build_chat_model(settings, role="extraction")
    assert extraction.model_name == "default-model"
    agent = build_strands_model(settings)
    assert agent.config["model_id"] == "agent-model"
    assert agent.client_args["base_url"] == "http://agent-host:11434/v1"
