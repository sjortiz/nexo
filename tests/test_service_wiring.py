"""The composition root builds each component from its own model role."""

from __future__ import annotations

import pytest

from nexo import service
from nexo.config import Settings


class StubGraphStore:
    """GraphStore stand-in so building a runtime needs no Neo4j."""

    def verify(self):
        """Accept the connectivity check."""

    def ensure_schema(self):
        """Accept the schema setup."""

    def describe_schema(self):
        """Return a fixed schema description."""
        return "schema"

    def close(self):
        """Accept the close call."""


@pytest.fixture(name="role_settings")
def fixture_role_settings(tmp_path, monkeypatch):
    """Settings with a distinct model per role, with telemetry and Neo4j stubbed out."""
    monkeypatch.setattr(
        service, "setup_telemetry", lambda settings: None
    )  # keep global OTEL/env untouched
    monkeypatch.setattr(
        service.GraphStore,
        "from_settings",
        classmethod(lambda cls, s, read_only: StubGraphStore()),
    )
    return Settings(
        _env_file=None,
        chunk_store="local",  # Neo4j is stubbed: keep chunks in-process
        embedding_provider="fake",
        checkpoint_backend="memory",
        vector_store_path=tmp_path / "v.json",
        vllm_model="default-model",
        agent_base_url="http://agent-host:11434/v1",
        agent_model="agent-model",
        extraction_model="extraction-model",
        router_model="router-model",
        retrieval_mode="keyword",
    )


def test_runtime_uses_router_and_agent_roles(role_settings, monkeypatch):
    """The router gets the router model; the investigator gets the agent model and endpoint."""
    built = {}
    original = service.GraphDependencies

    def capture(**kwargs):
        built.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(service, "GraphDependencies", capture)
    with service.open_runtime(role_settings) as runtime:
        assert {"router", "investigator", "compliance"} <= set(
            runtime.graph.get_graph().nodes
        )
    assert built["router_llm"].model_name == "router-model"
    # The composition root's private wiring is what this test checks.
    agent_model = built["investigator"]._model  # pylint: disable=protected-access
    assert agent_model.config["model_id"] == "agent-model"
    assert agent_model.client_args["base_url"] == "http://agent-host:11434/v1"
    chunks = built["investigator"]._chunks  # pylint: disable=protected-access
    assert chunks._mode == "keyword"  # pylint: disable=protected-access


def test_ingestor_and_resolver_use_the_extraction_role(role_settings):
    """The ingestor and its entity resolver share one extraction-model client."""
    with service.open_ingestor(role_settings) as ingestor:
        # Private wiring between ingestor and resolver is what this test checks.
        llm = ingestor._llm  # pylint: disable=protected-access
        assert llm.model_name == "extraction-model"
        assert ingestor._aids.resolver._llm is llm  # pylint: disable=protected-access
