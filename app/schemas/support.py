from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

ALLOWED_PRIORITIES = frozenset({'LOW', 'NORMAL', 'HIGH', 'URGENT'})
ALLOWED_STATUSES = frozenset({'open', 'closed', 'escalated'})


class SupportTicketCreate(BaseModel):
    subject: str = Field(..., min_length=2, max_length=200)
    # Frontend posts `message`; accept `description` / `body` for compatibility.
    message: str | None = Field(default=None, max_length=8000)
    description: str | None = Field(default=None, max_length=8000)
    body: str | None = Field(default=None, max_length=8000)
    priority: str = Field(default='NORMAL', max_length=16)

    @field_validator('priority')
    @classmethod
    def normalize_priority(cls, value: str) -> str:
        normalized = (value or 'NORMAL').strip().upper()
        if normalized not in ALLOWED_PRIORITIES:
            raise ValueError('priority must be LOW, NORMAL, HIGH, or URGENT')
        return normalized

    @model_validator(mode='after')
    def require_description(self) -> SupportTicketCreate:
        text = (self.message or self.description or self.body or '').strip()
        if len(text) < 1:
            raise ValueError('description is required')
        object.__setattr__(self, 'description', text)
        return self


class SupportTicketResponse(BaseModel):
    id: str
    org_id: str
    created_by: str
    sender: str
    subject: str
    description: str
    # Compatibility aliases for the current frontend components.
    message: str
    body: str
    priority: str
    status: str
    created_at: datetime
    updated_at: datetime


class SupportTicketListResponse(BaseModel):
    items: list[SupportTicketResponse]
    total: int


class SupportTicketStatusUpdate(BaseModel):
    status: str = Field(..., pattern='^(open|closed|escalated)$')
