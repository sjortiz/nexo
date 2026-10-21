"""LangGraph state and the structured messages that enter/leave it.

State values are plain JSON (dicts/strings) so any checkpointer can persist
them across the human-review pause, which can last hours.
"""

from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nexo.agents.models import RetrievalStrategy
from nexo.orchestration.compliance import ComplianceReport, Finding

RoutingTarget = Literal["investigator", "rejected"]
ApprovalStatus = Literal["pending", "approved", "rejected", "not_required"]


class GraphState(TypedDict, total=False):
    """State carried through the investigation workflow and its checkpoints."""

    # --- required by the workflow contract ---
    user_query: str
    # Append-only audit trail.
    agent_outputs: Annotated[list[dict[str, Any]], operator.add]
    routing_target: RoutingTarget
    approval_status: ApprovalStatus
    # --- supporting fields ---
    request_id: str
    caller_clearance: str  # set by the authenticated service layer, never by an LLM
    routing_reason: str
    investigation_scope: dict[str, Any]
    compliance_report: dict[str, Any]
    review: dict[str, Any]
    final_response: str | None


class RouteDecision(BaseModel):
    """Router LLM output (guided JSON)."""

    model_config = ConfigDict(extra="forbid")

    target: RoutingTarget
    reason: str = Field(max_length=300)
    strategy: RetrievalStrategy = "hybrid"
    doc_types: list[str] | None = Field(
        default=None, description="Only if the request restricts document types"
    )
    effective_from: date | None = None
    effective_to: date | None = None


class ReviewDecision(BaseModel):
    """What a human reviewer sends to resume the paused compliance node."""

    model_config = ConfigDict(extra="forbid")

    approval_status: Literal["approved", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)
    acknowledged_findings: bool = Field(
        default=False,
        description="Required to approve when the compliance report has blocking findings",
    )


def describe_errors(exc: ValidationError) -> str:
    """A ValidationError as one line for a person: `field: problem; ...`."""
    return "; ".join(
        f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
        for e in exc.errors(include_url=False)
    )


class ReviewPacket(BaseModel):
    """What the compliance node pauses with: the draft, its checks and how to resume."""

    request_id: str
    user_query: str
    draft_answer: str
    citations: list[str]
    compliance_report: ComplianceReport
    resume_with: dict[str, Any]  # ReviewDecision's JSON schema
    # Why the last resume was refused; left out of the JSON until one is.
    error: str | list[dict[str, Any]] | None = Field(
        default=None, exclude_if=lambda error: error is None
    )

    @property
    def blocking(self) -> bool:
        """Whether approving needs the reviewer to acknowledge the findings."""
        return self.compliance_report.blocking

    @property
    def findings(self) -> list[Finding]:
        """The compliance findings shown to the reviewer."""
        return self.compliance_report.findings


class ReviewPassage(BaseModel):
    """One cited chunk as a reviewer with some clearance may see it."""

    chunk_id: str
    source: str | None = None
    text: str | None = None  # None unless shown
    classification: str | None = None
    status: Literal["shown", "withheld", "not_found"]
