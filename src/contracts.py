from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


EvidenceStatus = Literal["verified", "missing", "conflicting", "stale", "inferred"]


class EvidenceItem(BaseModel):
    source: str = Field(description="Tool/data source name")
    finding: str = Field(description="Concise factual finding")
    reference: str | None = Field(default=None, description="Optional record ID / policy section / endpoint")
    # Added field (optional, defaults keep the starter harness compatible):
    # verified = read directly from a tool/data source; missing = could not be retrieved;
    # conflicting = two sources disagree; stale = exists but past its validity window;
    # inferred = derived by the model, never presented as a retrieved fact.
    status: EvidenceStatus = "verified"


class RunTelemetry(BaseModel):
    llm_calls: int | None = None
    tool_calls: int | None = None
    tool_names: list[str] = Field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Model rationale that the guard replaced with a template (kept for audit, never shown as the recommendation).
    rejected_rationale: str | None = None


class ProcurementDecision(BaseModel):
    request_id: str
    recommendation: str = Field(description="Short recommendation label or sentence")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    required_approvals: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_step: str
    human_review_required: bool = True
    # Added field: why a human has to look beyond routine approval (empty = routine approvals only).
    escalation_reasons: list[str] = Field(default_factory=list)
    telemetry: RunTelemetry | None = None


Architecture = Literal["single", "staged"]

# Suggested approval names for consistency in evaluation:
# Manager, Department Head, Procurement, Finance, CFO, Security, Privacy, Legal
#
# Suggested risk-flag taxonomy (you may add others):
# existing_tool_overlap
# budget_insufficient
# security_review_required
# privacy_review_required
# legal_review_required
# vendor_review_expired
# conflicting_vendor_evidence
# vendor_risk_unavailable
# prompt_injection_detected
# missing_information
