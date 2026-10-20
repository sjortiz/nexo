"""Deterministic pre-review checks shown to the human reviewer.

Pure functions of the agent output: the compliance node re-executes on
resume, so these must be idempotent and side-effect free.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from nexo.agents.models import AgentOutput
from nexo.storage.schemas import Sensitivity
from nexo.storage.sensitive_data import SensitiveDataDetector

Severity = Literal["block", "warn"]
ReviewPolicy = Literal["always", "on-findings", "never"]
AUTO_REVIEWER = "system:auto"


class Finding(BaseModel):
    """One compliance check result for the reviewer."""

    code: str
    severity: Severity
    detail: str


class ComplianceReport(BaseModel):
    """All findings for one agent output."""

    findings: list[Finding] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        """Whether any finding blocks the answer."""
        return any(f.severity == "block" for f in self.findings)

    def as_state(self) -> dict:
        """Return the report as a plain dict for the graph state."""
        return {
            "findings": [f.model_dump() for f in self.findings],
            "blocking": self.blocking,
        }


def run_compliance_checks(
    output: AgentOutput | None, detector: SensitiveDataDetector
) -> ComplianceReport:
    """Check the agent output for failures, grounding, sensitive data and budget issues."""
    report = ComplianceReport()
    add = report.findings.append
    if output is None or output.error:
        add(
            Finding(
                code="investigation_failed",
                severity="block",
                detail=output.error if output else "no investigator output",
            )
        )
        return report
    if not output.answer:
        add(
            Finding(
                code="empty_answer",
                severity="block",
                detail="investigator returned no text",
            )
        )
        return report

    if not output.citations:
        add(
            Finding(
                code="no_citations",
                severity="block",
                detail="answer cites no source chunks",
            )
        )
    ungrounded = [c for c in output.citations if c not in output.fetched_sensitivity]
    if ungrounded:
        # Cited ids whose text the agent never received: fabricated or guessed.
        add(
            Finding(
                code="ungrounded_citations",
                severity="block",
                detail=", ".join(ungrounded),
            )
        )

    cited_levels = {
        Sensitivity(output.fetched_sensitivity[c])
        for c in output.citations
        if c in output.fetched_sensitivity
    }
    if Sensitivity.PHI in cited_levels:
        add(
            Finding(
                code="phi_in_evidence",
                severity="block",
                detail=(
                    "answer relies on PHI-classified chunks; "
                    "confirm minimum-necessary disclosure"
                ),
            )
        )
    elif Sensitivity.PII in cited_levels:
        add(
            Finding(
                code="pii_in_evidence",
                severity="warn",
                detail="answer relies on PII-classified chunks",
            )
        )

    detected = sorted({d.category for d in detector.detect(output.answer)})
    if detected:
        add(
            Finding(
                code="sensitive_data_in_answer",
                severity="block",
                detail=", ".join(detected),
            )
        )

    if output.stop_reason.startswith("limit_"):
        add(
            Finding(
                code="budget_exhausted",
                severity="warn",
                detail=f"agent stopped on {output.stop_reason}; answer may be incomplete",
            )
        )
    failed_tools = [c for c in output.tool_calls if not c.ok]
    if failed_tools:
        add(
            Finding(
                code="tool_errors",
                severity="warn",
                detail=f"{len(failed_tools)} of {len(output.tool_calls)} tool calls failed",
            )
        )
    if output.withheld_chunk_ids:
        add(
            Finding(
                code="evidence_withheld",
                severity="warn",
                detail=(
                    f"{len(output.withheld_chunk_ids)} chunk(s) "
                    "above clearance or outside scope"
                ),
            )
        )
    return report


def grounded_citations(output: AgentOutput) -> bool:
    """The answer cites at least one chunk, and the agent read every chunk it cites."""
    return bool(output.citations) and all(
        c in output.fetched_sensitivity for c in output.citations
    )


def auto_approves(
    policy: ReviewPolicy, report: ComplianceReport, output: AgentOutput
) -> bool:
    """Whether the review policy releases this answer without a person (ADR-0003).

    Blocking findings always wait for a person, whatever the policy: approving them
    needs a reviewer who acknowledges them.
    """
    if report.blocking or output.error or not output.answer:
        return False
    if policy == "never":
        return True
    if policy == "on-findings":
        return not report.findings and grounded_citations(output)
    return False
