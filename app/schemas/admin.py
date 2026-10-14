from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ClientOrganizationCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    auth_org_id: str = Field(..., min_length=2, max_length=128)
    cco_user_id: str = Field(..., min_length=2, max_length=128)
    regulatory_scope: dict[str, Any] = Field(
        default_factory=lambda: {
            'sources': ['SEC', 'FINRA', 'CFTC', 'NFA'],
            'jurisdictions': ['US'],
            'products': [],
        }
    )
    approval_chain: dict[str, Any] = Field(default_factory=dict)
    escalation_days: int = Field(default=7, ge=1, le=90)


class ClientOrganizationResponse(BaseModel):
    id: str
    name: str
    auth_org_id: str
    cco_user_id: str
    regulatory_scope: dict[str, Any]
    approval_chain: dict[str, Any]
    escalation_days: int
    created_by: str
    persisted: bool
    cco_role: str
    role_assigned: bool
