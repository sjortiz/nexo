"""The composition root builds each component from its own model role."""

from __future__ import annotations

import pytest

import nexo.service as service
from nexo.config import Settings


class StubGraphStore:
    def verify(self):
        pass

    def ensure_schema(self):
        pass

    def describe_schema(self):
        return "schema"

    def close(self):
        pass


@pytest.fixture
def role_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "setup_telemetry", lambda settings: None)  # keep global OTEL/env untouched
    monkeypatch.setattr(service.GraphStore, "from_settings", classmethod(lambda cls, s, read_only: StubGraphStore()))
    return Settings(_env_file=None, embedding_provider="fake", checkpoint_backend="memory",
                    vector_store_path=tmp_path / "v.json", vllm_model="default-model",
                    agent_base_url="http://agent-host:11434/v1", agent_model="agent-model",
                    extraction_model="extraction-model", router_model="router-model")


def test_runtime_uses_router_and_agent_roles(role_settings, monkeypatch):
    built = {}
    original = service.GraphDependencies

    def capture(**kwargs):
        built.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service, "GraphDependencies", capture)
    with service.open_runtime(role_settings) as runtime:
        assert {"router", "investigator", "compliance"} <= set(runtime.graph.get_graph().nodes)
    assert built["router_llm"].model_name == "router-model"
    agent_model = built["investigator"]._model
    assert agent_model.config["model_id"] == "agent-model"
    assert agent_model.client_args["base_url"] == "http://agent-host:11434/v1"


def test_ingestor_and_resolver_use_the_extraction_role(role_settings):
    with service.open_ingestor(role_settings) as ingestor:
        assert ingestor._llm.model_name == "extraction-model"
        assert ingestor._resolver._llm is ingestor._llm
