from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.member_capabilities import require_delete_resources
from app.core.roles import can_manage_org_newsletter_subscribers
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.newsletter import (
    NewsletterInviteRequest,
    NewsletterInviteResponse,
    NewsletterSubscriberDeleteResponse,
    NewsletterSubscribeRequest,
    NewsletterSubscribeResponse,
    NewsletterSubscriberListResponse,
)
from app.schemas.regulatory import RegulatoryPublicationResponse
from app.services.newsletter_service import (
    NEWSLETTER_SOURCES,
    delete_org_newsletter_subscriber,
    invite_newsletter_subscriber,
    list_org_newsletter_subscribers,
    subscribe_newsletter,
)
from app.services.regulatory_monitoring_service import list_shared_feed_publications

router = APIRouter(prefix='/newsletter', tags=['newsletter'])


def require_newsletter_subscriber_manager(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not can_manage_org_newsletter_subscribers(user.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master and CCO users can manage newsletter subscribers',
        )
    return user


@router.get('/publications', response_model=list[RegulatoryPublicationResponse])
def get_newsletter_publications(
    limit: int = Query(default=50, ge=1, le=100),
) -> list[RegulatoryPublicationResponse]:
    """Public copy of the shared Regulatory Feed (SEC, FINRA, CFTC)."""
    return list_shared_feed_publications(sources=NEWSLETTER_SOURCES, limit=limit)


@router.get('/subscribers', response_model=NewsletterSubscriberListResponse)
def get_org_newsletter_subscribers(
    user: CurrentUser = Depends(require_newsletter_subscriber_manager),
) -> NewsletterSubscriberListResponse:
    """List this organization's private newsletter subscribers. Master and CCO only."""
    return list_org_newsletter_subscribers(user)


@router.delete(
    '/subscribers/{subscriber_id}',
    response_model=NewsletterSubscriberDeleteResponse,
)
def delete_org_newsletter_subscriber_route(
    subscriber_id: str,
    user: CurrentUser = Depends(require_delete_resources),
) -> NewsletterSubscriberDeleteResponse:
    """Remove one private org newsletter subscriber. Master and CCO only."""
    return delete_org_newsletter_subscriber(subscriber_id, user)


@router.post('/invites', response_model=NewsletterInviteResponse)
def post_newsletter_invite(
    payload: NewsletterInviteRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> NewsletterInviteResponse:
    """Any organization member can email a Reg-AI Newsletter invitation."""
    return invite_newsletter_subscriber(payload, user, db)


@router.post(
    '/subscribe',
    response_model=NewsletterSubscribeResponse,
    status_code=status.HTTP_200_OK,
)
def post_newsletter_subscribe(
    payload: NewsletterSubscribeRequest,
) -> NewsletterSubscribeResponse:
    """Public subscribe sends an invitation; accepting the link stores the address."""
    return subscribe_newsletter(payload)
