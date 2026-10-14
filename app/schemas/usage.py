from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class UsageIncrementRequest(BaseModel):
    metric: str = Field(..., min_length=1, max_length=80)
    amount: int = Field(default=1, ge=1, le=100_000)


class UsageCounterEntry(BaseModel):
    day: date
    metric: str
    count: int


class UsageSummaryResponse(BaseModel):
    org_id: str
    totals: dict[str, int]
    daily: list[UsageCounterEntry]
