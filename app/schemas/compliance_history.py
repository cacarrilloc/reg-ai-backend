from datetime import datetime

from pydantic import BaseModel, Field


class ComplianceHistoryResponse(BaseModel):
    id: str
    gap_analysis_id: str | None = None
    policy_id: str | None = None
    policy_name: str
    regulatory_rules: list[str] = Field(default_factory=list)
    trigger_type: str | None = None
    incident_at: datetime
    # Latest same-gap CCO review notes from audit_logs (PII-sanitized). Empty when none.
    cco_judgment_notes: str | None = None


class ComplianceHistoryListResponse(BaseModel):
    items: list[ComplianceHistoryResponse]
    total: int
    days: int
