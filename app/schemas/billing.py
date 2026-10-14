from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CheckoutSessionRequest(BaseModel):
    price_id: str | None = Field(default=None, max_length=128)
    plan_code: str | None = Field(default=None, max_length=32)
    success_url: str = Field(..., min_length=1, max_length=2048)
    cancel_url: str = Field(..., min_length=1, max_length=2048)


class CheckoutSessionResponse(BaseModel):
    checkout_url: str
    session_id: str | None = None


class PortalSessionRequest(BaseModel):
    return_url: str = Field(..., min_length=1, max_length=2048)


class PortalSessionResponse(BaseModel):
    portal_url: str


class BillingStatusResponse(BaseModel):
    auth_org_id: str
    has_customer: bool
    plan_code: str | None = None
    status: str = 'inactive'
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
