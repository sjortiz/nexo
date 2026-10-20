"""Shared fixtures and test doubles: tracing, settings, chunks, graph and chat model fakes."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import keyring
import keyring.backend
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from nexo.config import Settings
from nexo.llm import build_embeddings
from nexo.storage.graph_store import ReadResult
from nexo.storage.schemas import ChunkRecord, Sensitivity
from nexo.storage.vector_store import LocalChunkStore
from nexo.telemetry import instrument_openai

# Tests never reach a real keychain (it can block on an unlock prompt): keyring picks its
# backend on first use, so this applies here and in subprocesses; the fixture below
# installs an in-memory one per test.
os.environ.setdefault("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")

_EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_provider)
os.environ.pop("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", None)
instrument_openai(_provider)


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A keychain that lives for one test."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        self.items.pop((service, username), None)


@pytest.fixture(autouse=True)
def memory_keychain(tmp_path_factory, monkeypatch):
    """No test reads or writes the real keychain or ~/.config/nexo."""
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    for key in [k for k in os.environ if k.startswith("NEXO_")]:
        monkeypatch.delenv(key)
    # After the cleanup above: every test gets its own ~/.config/nexo.
    monkeypatch.setenv("NEXO_CONFIG_DIR", str(tmp_path_factory.mktemp("nexo-config")))
    yield backend
    keyring.set_keyring(previous)


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
        vector_store_path=tmp_path / "vectors.json",
        checkpoint_backend="memory",
        checkpoint_sqlite_path=tmp_path / "checkpoints.sqlite",
        extraction_concurrency=1,
        chunk_size=400,
        chunk_overlap=0,
        seed_chunks=2,
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


@pytest.fixture(name="chunk_store")
def fixture_chunk_store(settings) -> LocalChunkStore:
    """Provide an empty local chunk store backed by the fake embeddings."""
    return LocalChunkStore(build_embeddings(settings), settings.vector_store_path)


@dataclass
class FakeGraph:
    """Stands in for GraphStore.run_read_query; records the Cypher it receives."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None
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
