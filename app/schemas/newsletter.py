from datetime import datetime

from pydantic import BaseModel, Field


class NewsletterInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class NewsletterInviteResponse(BaseModel):
    status: str
    email: str


class NewsletterSubscribeRequest(BaseModel):
    email: str | None = Field(default=None, max_length=320)
    token: str | None = Field(default=None, max_length=2_000)


class NewsletterSubscribeResponse(BaseModel):
    status: str
    email: str
    already_subscribed: bool = False


class NewsletterSendResponse(BaseModel):
    status: str
    publication_count: int = 0
    recipient_count: int = 0
    sent_count: int = 0
    skipped_orgs: int = 0
    errors: list[str] = Field(default_factory=list)


class NewsletterSubscriberResponse(BaseModel):
    id: str
    email: str
    source: str
    invited_by: str | None = None
    subscribed_at: datetime | None = None


class NewsletterSubscriberListResponse(BaseModel):
    subscribers: list[NewsletterSubscriberResponse]
    total: int = 0


class NewsletterSubscriberDeleteResponse(BaseModel):
    status: str = 'deleted'
    id: str
    email: str
