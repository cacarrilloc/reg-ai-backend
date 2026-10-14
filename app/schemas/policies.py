from datetime import date, datetime

from pydantic import BaseModel, Field


class PolicyUploadResponse(BaseModel):
    policy_id: str
    org_id: str
    filename: str
    status: str = 'ingested'
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    embedding_model: str | None = None
    file_type: str | None = None
    persisted: bool = False
    version: int = 1
    file_url: str | None = None


class PolicySummary(BaseModel):
    id: str
    org_id: str
    name: str
    type: str = 'uploaded'
    version: int = 1
    status: str = 'ACTIVE'
    file_url: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version_count: int = 1


class PolicyNameSuggestion(BaseModel):
    id: str
    name: str
    version: int
    similarity: float


class PolicyNameCheckResponse(BaseModel):
    normalized_name: str
    exact_match: PolicySummary | None = None
    similar_names: list[PolicyNameSuggestion] = Field(default_factory=list)


class PolicyFileDownloadResponse(BaseModel):
    url: str
    expires_in: int


class ComplianceReportResponse(BaseModel):
    report_id: str
    org_id: str
    file_url: str
    download_url: str
    expires_in: int
    policy_count: int
    gap_count: int
    compliance_status: str
    compliance_score: float
    generated_at: datetime


class BusinessChangeAnalysisDownloadResponse(BaseModel):
    change_id: str
    analysis_status: str
    file_url: str
    download_url: str
    expires_in: int
    generated_at: datetime | None = None


class BusinessChangeCreate(BaseModel):
    change_type: str = Field(..., examples=['NEW_PRODUCT', 'NEW_PROCESS', 'NEW_HIRE', 'OTHER'])
    description: str = Field(..., min_length=10)
    change_date: date | None = None


class BusinessChangeResponse(BaseModel):
    id: str
    org_id: str
    change_type: str
    description: str
    change_date: date | None = None
    triggers_review: bool
    assessment_rationale: str = ''
    event_summary: str = ''
    assessment_mode: str = 'RULE_FALLBACK'
    assessment_model: str | None = None
    persisted: bool = False
    event_emitted: bool = False
    flagged_at: datetime | None = None
    analysis_status: str = 'PENDING'
    analysis_generated_at: datetime | None = None


class BusinessChangeListItem(BaseModel):
    id: str
    org_id: str
    change_type: str
    description: str
    change_date: date | None = None
    triggers_review: bool
    assessment_rationale: str = ''
    event_summary: str = ''
    assessment_mode: str = 'RULE_FALLBACK'
    flagged_at: datetime | None = None
    analysis_status: str = 'PENDING'
    analysis_generated_at: datetime | None = None
    gap_analysis_id: str | None = None
    gap_status: str | None = None
    gap_redline_eligible: bool = False
