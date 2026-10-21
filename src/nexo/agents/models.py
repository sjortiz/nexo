"""Data exchanged across the LangGraph <-> Strands boundary (JSON-serialisable)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from nexo.storage.vector_store import ChunkFilter

RetrievalStrategy = Literal["graph", "vector", "hybrid"]

AGENT_NAME = "relationship-investigator"


@dataclass(frozen=True)
class AgentBudget:
    """What one investigation may spend; the service builds it once from Settings."""

    max_turns: int
    max_tool_calls: int
    max_total_tokens: int
    timeout_s: float
    seed_chunks: int
    max_chunks_per_call: int
    tool_result_max_chars: int

    @classmethod
    def from_settings(cls, settings: Any) -> AgentBudget:
        """The budget the AGENT_* and SEED_CHUNKS settings describe."""
        return cls(
            max_turns=settings.agent_max_turns,
            max_tool_calls=settings.agent_max_tool_calls,
            max_total_tokens=settings.agent_max_total_tokens,
            timeout_s=settings.agent_timeout_s,
            seed_chunks=settings.seed_chunks,
            max_chunks_per_call=settings.agent_max_chunks_per_call,
            tool_result_max_chars=settings.tool_result_max_chars,
        )


class InvestigationScope(BaseModel):
    """Set by the router: where to start and what the caller may see."""

    strategy: RetrievalStrategy = "hybrid"
    chunk_filter: ChunkFilter = Field(default_factory=ChunkFilter)


class ToolCallSummary(BaseModel):
    """Audit record of one agent tool call."""

    tool: str
    ok: bool
    refused: bool = False  # asked for after the tool budget was spent
    detail: str = ""
    duration_ms: float = 0.0


class AgentOutput(BaseModel):
    """The investigator's answer, citations, evidence ledger and run metadata."""

    agent: str = AGENT_NAME
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
