"""Shared fixtures and test doubles: tracing, settings, chunks, graph and chat model fakes."""

from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from strands.models.openai import OpenAIModel

from nexo.config import Settings
from nexo.llm import build_embeddings
from nexo.storage.graph_store import ReadResult
from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.vector_store import LocalChunkStore
from nexo.telemetry import BaggageAttributes, instrument_openai

_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(BaggageAttributes("langfuse."))  # as setup_telemetry does
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)
os.environ.pop("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", None)
instrument_openai(_provider)


class MemoryKeyring:
    """A keychain that lives for one test (keyring's get/set/delete_password interface)."""

    def __init__(self):
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        """The stored value, or None."""
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        """Store a value."""
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        """Forget a value."""
        self.items.pop((service, username), None)


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(name="workdir")
def fixture_workdir(tmp_path: Path) -> Path:
    """A checkout-like folder: the repo's deploy/ fragments, its own .nexo and .env."""
    (tmp_path / "deploy").symlink_to(ROOT / "deploy")
    return tmp_path


@pytest.fixture(autouse=True)
def isolated_config(tmp_path_factory, monkeypatch):
    """No test reads or writes ~/.config/nexo or sees NEXO_* settings from the shell."""
    for key in [k for k in os.environ if k.startswith("NEXO_")]:
        monkeypatch.delenv(key)
    # After the cleanup above: every test gets its own ~/.config/nexo.
    monkeypatch.setenv("NEXO_CONFIG_DIR", str(tmp_path_factory.mktemp("nexo-config")))
    monkeypatch.setenv("NEXO_DATA_DIR", str(tmp_path_factory.mktemp("nexo-data")))


@pytest.fixture
def memory_keychain() -> MemoryKeyring:
    """A fake keychain to inject: KeychainStore(stack, backend=memory_keychain)."""
    return MemoryKeyring()


@pytest.fixture
def spans() -> InMemorySpanExporter:
    """Provide the global in-memory span exporter, emptied for this test."""
    _EXPORTER.clear()
    return _EXPORTER


@pytest.fixture(name="settings")
def fixture_settings(tmp_path) -> Settings:
    """Provide offline settings: fake embeddings, memory checkpoints, files under tmp_path."""
    return Settings(
        _env_file=None,
        embedding_provider="fake",
        embedding_dimensions=16,
        chunk_store="local",
        vector_store_path=tmp_path / "vectors.json",
        checkpoint_backend="memory",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite",
        extraction_concurrency=1,
        chunk_size=400,
        chunk_overlap=0,
        seed_chunks=2,
        # The review tests exercise the pause; test_review_policy covers the default.
        review_policy="always",
    )


def make_chunk(
    source_id: str,
    ordinal: int,
    text: str,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    **extra: Any,
) -> ChunkRecord:
    """Build a chunk with a content-derived id and the current ingest time."""
    return ChunkRecord(
        chunk_id=ChunkRecord.make_id(source_id, ordinal, text),
        source_id=source_id,
        ordinal=ordinal,
        text=text,
        sensitivity=sensitivity,
        ingested_at=datetime.now(UTC),
        **extra,
    )


def model_text(result: dict[str, Any]) -> str:
    """The text a tool result shows the model, written by Strands' OpenAI provider."""
    message = OpenAIModel.format_request_tool_message({"toolUseId": "t", **result})
    return message["content"]


def as_model_text(agent_tool: Any) -> Any:
    """Call agent_tool directly and get back the text the model would read."""
    return lambda *args, **kwargs: model_text(agent_tool(*args, **kwargs))


@pytest.fixture(name="chunk_store")
def fixture_chunk_store(settings) -> LocalChunkStore:
    """Provide an empty local chunk store backed by the fake embeddings."""
    return LocalChunkStore(build_embeddings(settings), settings.vector_store_path)


@dataclass
class FakeGraph:
    """Stands in for GraphStore.run_read_query; records the Cypher it receives."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None
    # Ranks the path endpoint lookup reports per key; keys left out are rank 1.
    endpoints: dict[str, int | None] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list, init=False)
    parameters: list[dict[str, Any] | None] = field(default_factory=list, init=False)

    def run_read_query(
        self, cypher: str, parameters: dict[str, Any] | None = None
    ) -> ReadResult:
        """Record the query and its parameters, then raise the error or return the rows."""
        self.queries.append(cypher)
        self.parameters.append(parameters)
        if self.error:
            raise self.error
        if "collect(e {.key, .sensitivity_rank})" in cypher:  # the endpoint lookup
            entities = [
                {"key": k, "sensitivity_rank": self.endpoints.get(k, 1)}
                for k in parameters["keys"]
                if k not in self.endpoints or self.endpoints[k] != -1
            ]
            return ReadResult(rows=[{"entities": entities}], truncated=False)
        return ReadResult(rows=self.rows, truncated=False)


# Streaming and tool binding are optional in BaseChatModel (they raise NotImplementedError).
class RecordingChatModel(BaseChatModel):  # pylint: disable=abstract-method
    """Scripted chat model that records every prompt it receives."""

    replies: list[str]
    prompts: list[list[Any]] = []

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.prompts.append(list(messages))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=reply))]
        )
