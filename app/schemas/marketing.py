from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class MarketingUploadUrlRequest(BaseModel):
    filename: str
    content_type: str | None = None
    size: int


class MarketingUploadUrlResponse(BaseModel):
    content_id: str
    path: str
    signed_url: str
    token: str


class MarketingContentResponse(BaseModel):
    id: str
    org_id: str
    name: str
    storage_path: str | None = None
    status: str
    uploaded_by: str | None = None
    created_at: datetime
    title: str | None = None
    format: str = 'text'
    channel: str | None = None
    audience: str = 'unknown'
    registrant_type: str = 'unknown'
    source_url: str | None = None
    body_text: str | None = None
    content_tags: list[str] = Field(default_factory=list)
    layout_notes: list[dict[str, Any]] = Field(default_factory=list)
    classification: dict[str, Any] = Field(default_factory=dict)
    published_locations: list[str] = Field(default_factory=list)
    cadence_key: str | None = None
    cadence_step: int | None = None
    latest_review_id: str | None = None
    latest_review_status: str | None = None
    cco_review_in_progress: bool = False
    cosine_similarity: float | None = None


class MarketingContentListResponse(BaseModel):
    items: list[MarketingContentResponse]
    total: int


class MarketingContentDetailResponse(MarketingContentResponse):
    html_preview: str | None = None
    latest_review: MarketingReviewResponse | None = None


class MarketingReviewResponse(BaseModel):
    id: str
    org_id: str
    content_id: str
    status: str
    findings: list[dict[str, Any]]
    decided_by: str | None = None
    decided_at: datetime | None = None
    created_at: datetime
    mode: str = 'A'
    trigger_type: str | None = None
    trigger_id: str | None = None
    publication_id: str | None = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    answers: dict[str, Any] = Field(default_factory=dict)
    layout_notes: list[dict[str, Any]] = Field(default_factory=list)
    classification: dict[str, Any] = Field(default_factory=dict)
    rule_versions: dict[str, Any] = Field(default_factory=dict)
    change_list: list[dict[str, Any]] = Field(default_factory=list)
    selected_options: list[dict[str, Any]] = Field(default_factory=list)
    output_kind: str | None = None
    artifact_storage_path: str | None = None
    skipped_archive_count: int = 0
    error_message: str | None = None
    report_header: str | None = None
    performance_disclaimer: str | None = None
    cco_revision_id: str | None = None
    cco_policy_name: str | None = None


class MarketingRedlinePrepareResponse(BaseModel):
    revision_id: str
    policy_name: str


class MarketingReviewDecision(BaseModel):
    """CCO sign-off of options. approved/rejected is accepted only as legacy alias for signed_off."""

    status: str | None = Field(default=None, pattern='^(approved|rejected|signed_off)$')
    findings: list[dict[str, Any]] | None = None
    selected_options: list[dict[str, str]] = Field(default_factory=list)


class MarketingReviewAnswers(BaseModel):
    answers: dict[str, str] = Field(default_factory=dict)


class MarketingArchiveTagResponse(BaseModel):
    tagged: int
    skipped: int


MarketingContentDetailResponse.model_rebuild()
