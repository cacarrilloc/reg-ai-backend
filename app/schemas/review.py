from datetime import date, datetime

from pydantic import BaseModel, Field, model_validator

from app.schemas.gap_analyses import GapCitationResponse, GapFindingSummary

NOTES_REQUIRED_DECISIONS = frozenset(
    {
        'REJECTED',
        'APPROVED_WITH_EDITS',
        # Legacy alias treated as approve-with-edits.
        'APPROVED',
    }
)


class ReviewDecisionRequest(BaseModel):
    """Request schema for submitting a review decision."""

    decision: str = Field(
        ...,
        pattern='^(APPROVED|APPROVED_WITH_EDITS|APPROVED_ALL_CHANGES|REJECTED|APPROVE_REVIEW|REJECT_REVIEW|PARTIALLY_ACCEPTED)$',
        description=(
            'Policy: APPROVED and APPROVED_WITH_EDITS require a manual Policy Library upload. '
            'APPROVED_ALL_CHANGES accepts all redline tracked changes and auto-uploads '
            'a clean policy document. Marketing: APPROVE_REVIEW, REJECT_REVIEW, and '
            'PARTIALLY_ACCEPTED record the CCO outcome and move the item to the Audit trail '
            'without a Policy Library upload. Review notes are required for REJECTED and '
            'APPROVED_WITH_EDITS. Marketing decisions may include optional notes.'
        ),
    )
    notes: str | None = None

    @model_validator(mode='after')
    def require_notes_for_reject_and_edits(self) -> 'ReviewDecisionRequest':
        decision = (self.decision or '').strip().upper()
        notes = ' '.join((self.notes or '').split()).strip()
        if decision in NOTES_REQUIRED_DECISIONS and not notes:
            raise ValueError(
                'Review notes are required for this decision.'
            )
        # Cap length; empty optional notes for Approve all changes stay None.
        self.notes = notes[:4_000] if notes else None
        return self


class ReviewDecisionResponse(BaseModel):
    revision_id: str
    status: str
    audit_log_id: str
    policy_version: int | None = None
    approved_file_url: str | None = None
    policy_id: str | None = None
    policy_name: str | None = None
    requires_policy_upload: bool = False
    approval_mode: str | None = None


class AssignReviewerRequest(BaseModel):
    """Assign a reviewer for a pending revision and email a Policy Review Request."""

    user_id: str = Field(..., min_length=2, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    full_name: str | None = Field(default=None, max_length=160)
    escalate: bool = False


class AssignReviewerResponse(BaseModel):
    revision_id: str
    policy_name: str
    reviewer_user_id: str
    reviewer_email: str | None = None
    reviewer_name: str
    email_status: str
    email_subject: str | None = None
    email_error: str | None = None
    email_kind: str = "policy_review_request"
    persisted: bool = True
    escalated: bool = False
    escalated_at: datetime | None = None


class PolicyRevisionResponse(BaseModel):
    id: str
    gap_analysis_id: str
    policy_id: str
    org_id: str
    redline_file_url: str | None = None
    status: str
    reviewer_id: str | None = None
    reviewed_at: datetime | None = None
    approved_file_url: str | None = None
    created_at: datetime
    persisted: bool = True


class GapSummaryResponse(BaseModel):
    """Gap analysis summary for review."""

    gap_analysis_id: str
    policy_id: str
    policy_name: str
    impact_score: str
    gap_summary: str
    rule: str | None = None
    fix: str | None = None
    regulatory_citations: list[GapCitationResponse] = Field(default_factory=list)
    trigger_type: str


class PendingRevisionResponse(BaseModel):
    """Pending revision for review dashboard."""

    revision_id: str
    gap_analysis_id: str
    policy_id: str
    policy_name: str
    impact_score: str
    trigger_type: str
    trigger_label: str
    regulatory_publication: str | None = None
    business_change: str | None = None
    change_date: date | datetime
    gap_summary: str
    rule: str | None = None
    fix: str | None = None
    gap_count: int = 0
    gaps: list[GapFindingSummary] = Field(default_factory=list)
    created_at: datetime
    days_pending: int
    redline_url: str | None = None
    redline_ready: bool = False
    reviewer_user_id: str | None = None
    reviewer_name: str | None = None
    reviewer_email: str | None = None
    has_assigned_reviewer: bool = False
    sla_reminded_at: datetime | None = None
    escalated_at: datetime | None = None
    escalation_days: int | None = None
    sla_reminder_days: int | None = None
    redline_internal_cosine_similarity: float | None = None
    redline_internal_policy_name: str | None = None
    redline_regulator_cosine_similarity: float | None = None
    redline_regulator_reference_policy_name: str | None = None
    marketing_rule_labels: list[str] = Field(default_factory=list)


class ReviewDashboardResponse(BaseModel):
    """CCO review dashboard data."""

    pending_revisions: list[PendingRevisionResponse]
    total_pending: int
    high_impact_count: int
    escalated_count: int


class PolicyAuditChainResponse(BaseModel):
    """Full trigger-to-decision chain for one policy gap."""

    policy_id: str
    policy_name: str
    policy_version: int
    trigger_type: str
    trigger_label: str
    trigger_id: str
    gap_analysis_id: str
    gap_summary: str
    impact_score: str
    rule: str | None = None
    gaps: list[GapFindingSummary] = Field(default_factory=list)
    regulatory_citations: list[GapCitationResponse] = Field(default_factory=list)
    regulator_reference_policy_name: str | None = None
    created_at: datetime
    change_date: date | datetime | None = None
    redline_revision_id: str | None = None
    redline_status: str | None = None
    redline_created_at: datetime | None = None
    reviewer_id: str | None = None
    decision: str | None = None
    decision_at: datetime | None = None
    decision_notes: str | None = None
    audit_log_id: str | None = None
    approved_file_url: str | None = None


class PolicyAuditChainListResponse(BaseModel):
    """Audit trail rows grouped by the frontend into per-policy chains."""

    chains: list[PolicyAuditChainResponse]
    total_chains: int


class ReviewHistoryResponse(BaseModel):
    """Completed CCO review decisions moved out of the pending queue."""

    decisions: list[PolicyAuditChainResponse]
    total_decisions: int


class AuditLogResponse(BaseModel):
    """Audit log entry for decision tracking."""

    id: str
    event_type: str
    actor_id: str
    timestamp: datetime
    revision_id: str | None = None
    policy_id: str | None = None
    gap_analysis_id: str | None = None
    notes: str | None = None
    metadata: dict = Field(default_factory=dict)
