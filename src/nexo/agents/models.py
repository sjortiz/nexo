"""Data exchanged across the LangGraph <-> Strands boundary (JSON-serialisable)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nexo.storage.vector_store import ChunkFilter

RetrievalStrategy = Literal["graph", "vector", "hybrid"]


class InvestigationScope(BaseModel):
    """Set by the router: where to start and what the caller may see."""

    strategy: RetrievalStrategy = "hybrid"
    chunk_filter: ChunkFilter = Field(default_factory=ChunkFilter)


class ToolCallSummary(BaseModel):
    """Audit record of one agent tool call."""

    tool: str
    ok: bool
    detail: str = ""
    duration_ms: float = 0.0


class AgentOutput(BaseModel):
    """The investigator's answer, citations, evidence ledger and run metadata."""

    agent: str = "strands-relationship-investigator"
    answer: str = ""
    citations: list[str] = Field(default_factory=list)
    seed_chunk_ids: list[str] = Field(default_factory=list)
    fetched_chunk_ids: list[str] = Field(default_factory=list)
    fetched_sensitivity: dict[str, str] = Field(default_factory=dict)
    withheld_chunk_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallSummary] = Field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, int] = Field(default_factory=dict)
    error: str | None = None
