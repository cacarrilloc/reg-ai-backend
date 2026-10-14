from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RegulatoryPublicationResponse(BaseModel):
    """A publication surfaced in the regulatory monitoring feed."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    source: str
    title: str
    url: str
    published_at: datetime | None = None
    processed: bool
    # Feed-facing state: ingested | matched.
    # ``processed`` stays for compatibility (true for both terminal feed states).
    status: str = "ingested"
    summary: str | None = None
    created_at: datetime


class RegulatorySyncResponse(BaseModel):
    """Immediate response after queueing a regulator scrape from the UI."""

    status: str
    message: str


class RegulatoryIngestResponse(BaseModel):
    """Summary returned after manually starting one or more source ingests."""

    status: str
    sources: list[str]
    fetched_count: int = Field(ge=0)
    created_count: int = Field(ge=0)
    duplicate_count: int = Field(ge=0)
    embedded_count: int = Field(default=0, ge=0)
    reference_policy_sync_count: int = Field(default=0, ge=0)
    emitted_event_count: int = Field(ge=0)
    errors: list[str] = Field(default_factory=list)
