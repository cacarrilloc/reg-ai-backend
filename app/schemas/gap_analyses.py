from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

GapImpactScore = Literal['HIGH', 'MEDIUM', 'INFO']
GapTriggerType = Literal['publication', 'business_change', 'policy_update', 'marketing_content']
GapWorkflowStatus = Literal['ACTIVE', 'IN_REVIEW', 'UPLOAD_PENDING']
KNOWN_GAP_TRIGGER_TYPES = ('publication', 'business_change', 'policy_update', 'marketing_content')


class GapCitationResponse(BaseModel):
    """A source citation returned by the policy-gap agent."""

    source: str
    reference: str
    quote: str | None = None
    rationale: str | None = None


class GapFindingSummary(BaseModel):
    """One sentence-level gap for dropdown listing in Gap Analysis / Review Queue."""

    summary: str
    rule: str | None = None
    fix: str | None = None
    impact_score: GapImpactScore = 'MEDIUM'
    sentence: str | None = None
    sentence_label: str | None = None
    display_kind: Literal['policy', 'marketing'] | None = None
    finding: str | None = None
    why: str | None = None
    confidence: str | None = None
    options: list[str] = Field(default_factory=list)
    options_display: str | None = None


class GapAnalysisResponse(BaseModel):
    """CCO-facing view of one persisted policy-gap result."""

    id: str
    policy_id: str
    policy_name: str
    policy_version: int
    impact_score: GapImpactScore
    trigger_type: GapTriggerType
    trigger_id: str
    trigger_label: str
    business_change_type: str | None = None
    gap_summary: str
    rule: str | None = None
    fix: str | None = None
    gap_count: int = 0
    gaps: list[GapFindingSummary] = Field(default_factory=list)
    citations: list[GapCitationResponse] = Field(default_factory=list)
    retrieval_mode: str
    analysis_mode: str
    analysis_model: str | None = None
    regulator_cosine_similarity: float | None = None
    regulator_reference_policy_name: str | None = None
    business_change_cosine_similarity: float | None = None
    created_at: datetime
    status: GapWorkflowStatus = 'ACTIVE'
    revision_id: str | None = None
    redline_ready: bool = False
    redline_eligible: bool = True
    new_policy_from_business_change: bool = False
    needs_review_decision: bool = False
    needs_review_decision_message: str | None = None
    marketing_format: str | None = None
    marketing_format_label: str | None = None
    marketing_rule_labels: list[str] = Field(default_factory=list)
