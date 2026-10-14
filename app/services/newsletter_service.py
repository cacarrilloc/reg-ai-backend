"""Shared Reg-AI Newsletter: invitations, public subscribe, weekly send."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import structlog
from fastapi import HTTPException, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.db.session import platform_session_scope, session_scope, use_shared_search_path
from app.models.core import (
    GeneralNewsletterSubscriber,
    NewsletterSubscriber,
    Organization,
    OrganizationTenant,
    RegulatoryPublication,
)
from app.schemas.newsletter import (
    NewsletterInviteRequest,
    NewsletterInviteResponse,
    NewsletterSendResponse,
    NewsletterSubscriberDeleteResponse,
    NewsletterSubscribeRequest,
    NewsletterSubscribeResponse,
    NewsletterSubscriberListResponse,
    NewsletterSubscriberResponse,
)
from app.services.newsletter_html import (
    build_newsletter_invite_email_html,
    build_weekly_newsletter_html,
    classify_publication_kind,
    format_brief_date,
    looks_like_comment_period,
)
from app.services.notification_service import send_resend_email
from app.services.publication_summary_service import publication_text_is_title_only
from app.services.regulatory_monitoring_service import (
    newest_publication_for_source,
    publication_summary,
)

logger = structlog.get_logger(__name__)

NEWSLETTER_SOURCES = ('SEC', 'FINRA', 'CFTC')
TOKEN_TTL_DAYS = 14
NEWSLETTER_LIST_PUBLIC = 'public'
NEWSLETTER_LIST_ORG = 'org'
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
_PENDING_FEED_SUMMARY = 'Full text pending scrape.'
_SENTINEL_ORG_IDS = frozenset({'', 'platform'})


@dataclass(frozen=True)
class NewsletterInviteClaims:
    """Decoded invitation: public signups vs an organization's private list."""

    email: str
    org_id: str
    list_kind: str
    invited_by: str | None = None


def _real_org_id(org_id: str | None) -> str | None:
    cleaned = (org_id or '').strip()
    if not cleaned or cleaned.lower() in _SENTINEL_ORG_IDS:
        return None
    return cleaned


def _normalize_list_kind(list_kind: str | None, *, org_id: str | None) -> str:
    cleaned = (list_kind or '').strip().lower()
    if cleaned in {NEWSLETTER_LIST_PUBLIC, NEWSLETTER_LIST_ORG}:
        return cleaned
    return NEWSLETTER_LIST_ORG if _real_org_id(org_id) else NEWSLETTER_LIST_PUBLIC


def normalize_newsletter_email(value: str | None) -> str:
    cleaned = (value or '').strip().lower()
    if not cleaned or not _EMAIL_RE.fullmatch(cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Enter a valid email address',
        )
    return cleaned


def create_newsletter_invite_token(
    *,
    email: str,
    org_id: str = '',
    list_kind: str | None = None,
    invited_by: str | None = None,
    expires_at: datetime | None = None,
    settings: Settings | None = None,
) -> str:
    exp = expires_at or (datetime.now(UTC) + timedelta(days=TOKEN_TTL_DAYS))
    resolved_list = _normalize_list_kind(list_kind, org_id=org_id)
    resolved_org = _real_org_id(org_id) if resolved_list == NEWSLETTER_LIST_ORG else None
    payload = {
        'e': normalize_newsletter_email(email),
        'o': resolved_org or '',
        'l': resolved_list,
        'by': (invited_by or '').strip(),
        'exp': int(exp.timestamp()),
    }
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode('utf-8')
    ).decode('ascii').rstrip('=')
    signature = hmac.new(
        _token_secret_bytes(settings),
        raw.encode('ascii'),
        hashlib.sha256,
    ).hexdigest()
    return f'{raw}.{signature}'


def parse_newsletter_invite_token(
    token: str,
    *,
    settings: Settings | None = None,
) -> NewsletterInviteClaims:
    cleaned = (token or '').strip()
    try:
        raw, signature = cleaned.rsplit('.', 1)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This invitation link is invalid.',
        ) from exc
    expected = hmac.new(
        _token_secret_bytes(settings),
        raw.encode('ascii'),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This invitation link is invalid.',
        )
    padded = raw + '=' * (-len(raw) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This invitation link is invalid.',
        ) from exc
    try:
        expires = int(payload['exp'])
        email = normalize_newsletter_email(str(payload.get('e') or ''))
        org_id = str(payload.get('o') or '').strip()
        list_kind = _normalize_list_kind(str(payload.get('l') or ''), org_id=org_id)
        invited_by = str(payload.get('by') or '').strip() or None
    except (HTTPException, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This invitation link is invalid.',
        ) from exc
    if expires < datetime.now(UTC).timestamp():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This invitation link has expired.',
        )
    if list_kind == NEWSLETTER_LIST_PUBLIC:
        org_id = ''
    return NewsletterInviteClaims(
        email=email,
        org_id=org_id,
        list_kind=list_kind,
        invited_by=invited_by,
    )


def _send_newsletter_invitation(
    *,
    email: str,
    org_id: str,
    organization_name: str | None,
    settings: Settings,
    public_signup: bool = False,
    list_kind: str | None = None,
    invited_by: str | None = None,
) -> str:
    """Email an accept link. Does not store the address until the link is used."""
    resolved_list = (
        NEWSLETTER_LIST_PUBLIC
        if public_signup
        else _normalize_list_kind(list_kind, org_id=org_id)
    )
    token = create_newsletter_invite_token(
        email=email,
        org_id='' if resolved_list == NEWSLETTER_LIST_PUBLIC else org_id,
        list_kind=resolved_list,
        invited_by=None if resolved_list == NEWSLETTER_LIST_PUBLIC else invited_by,
        settings=settings,
    )
    accept_url = f"{settings.public_frontend_url.rstrip('/')}/newsletter?token={token}"
    result = send_resend_email(
        to_email=email,
        subject="You're invited to the Reg-AI Newsletter",
        html=build_newsletter_invite_email_html(
            accept_url=accept_url,
            organization_name=organization_name,
            invitee_email=email,
            public_signup=public_signup,
        ),
    )
    send_status = result.get('status')
    if send_status == 'sent':
        return 'sent'
    if send_status == 'skipped':
        logger.warning(
            'newsletter_invite_email_skipped',
            reason=result.get('reason'),
        )
        return 'queued'
    reason = result.get('error') or result.get('reason') or 'Unknown email delivery error'
    logger.warning('newsletter_invite_email_failed', reason=reason)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f'Invitation was not emailed to {email}: {reason}',
    )


def invite_newsletter_subscriber(
    payload: NewsletterInviteRequest,
    user: CurrentUser,
    db: Session | None,
    *,
    settings: Settings | None = None,
) -> NewsletterInviteResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Newsletter invitations require a configured database',
        )
    org_id = _real_org_id(user.org_id)
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Select an organization before inviting newsletter subscribers',
        )
    email = normalize_newsletter_email(payload.email)
    cfg = settings or get_settings()
    send_status = _send_newsletter_invitation(
        email=email,
        org_id=org_id,
        organization_name=_organization_name(db, org_id),
        settings=cfg,
        list_kind=NEWSLETTER_LIST_ORG,
        invited_by=user.user_id,
    )
    logger.info(
        'newsletter_invite_sent',
        org_id=org_id,
        invited_by=user.user_id,
        send_status=send_status,
    )
    return NewsletterInviteResponse(status=send_status, email=email)


def _org_id_or_400(user: CurrentUser) -> str:
    org_id = _real_org_id(user.org_id)
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Select an organization to manage newsletter subscribers',
        )
    return org_id


def _subscriber_response(row: NewsletterSubscriber) -> NewsletterSubscriberResponse:
    return NewsletterSubscriberResponse(
        id=row.id,
        email=row.email,
        source=(row.source or 'invite').strip() or 'invite',
        invited_by=row.invited_by,
        subscribed_at=row.subscribed_at,
    )


def list_org_newsletter_subscribers(user: CurrentUser) -> NewsletterSubscriberListResponse:
    """Return this organization's private newsletter list (not the public signup table)."""
    org_id = _org_id_or_400(user)
    with session_scope(org_id=org_id, commit=False) as db:
        rows = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.auth_org_id == org_id)
            .order_by(
                NewsletterSubscriber.subscribed_at.desc(),
                NewsletterSubscriber.email.asc(),
            )
            .all()
        )
        subscribers = [_subscriber_response(row) for row in rows]
    return NewsletterSubscriberListResponse(
        subscribers=subscribers,
        total=len(subscribers),
    )


def delete_org_newsletter_subscriber(
    subscriber_id: str,
    user: CurrentUser,
) -> NewsletterSubscriberDeleteResponse:
    """Remove one address from this organization's private newsletter list."""
    org_id = _org_id_or_400(user)
    cleaned_id = (subscriber_id or '').strip()
    if not cleaned_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='subscriber_id is required',
        )
    with session_scope(org_id=org_id) as db:
        row = (
            db.query(NewsletterSubscriber)
            .filter(NewsletterSubscriber.id == cleaned_id)
            .filter(NewsletterSubscriber.auth_org_id == org_id)
            .first()
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Newsletter subscriber not found',
            )
        email = row.email
        db.delete(row)
    logger.info(
        'newsletter_subscriber_deleted',
        org_id=org_id,
        subscriber_id=cleaned_id,
        actor_id=user.user_id,
    )
    return NewsletterSubscriberDeleteResponse(status='deleted', id=cleaned_id, email=email)


def subscribe_newsletter(
    payload: NewsletterSubscribeRequest,
    *,
    settings: Settings | None = None,
) -> NewsletterSubscribeResponse:
    token = (payload.token or '').strip()
    if token:
        claims = parse_newsletter_invite_token(token, settings=settings)
        if claims.list_kind == NEWSLETTER_LIST_ORG:
            org_id = _real_org_id(claims.org_id)
            if not org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail='This organization invitation is missing its organization.',
                )
            created = _persist_subscriber(
                email=claims.email,
                org_id=org_id,
                source='invite',
                invited_by=claims.invited_by,
                list_kind=NEWSLETTER_LIST_ORG,
            )
        else:
            created = _persist_subscriber(
                email=claims.email,
                org_id=None,
                source='public',
                list_kind=NEWSLETTER_LIST_PUBLIC,
            )
        return NewsletterSubscribeResponse(
            status='subscribed',
            email=claims.email,
            already_subscribed=not created,
        )

    email = normalize_newsletter_email(payload.email)
    cfg = settings or get_settings()
    send_status = _send_newsletter_invitation(
        email=email,
        org_id='',
        organization_name=None,
        settings=cfg,
        public_signup=True,
        list_kind=NEWSLETTER_LIST_PUBLIC,
    )
    logger.info('newsletter_public_invite_sent', send_status=send_status)
    return NewsletterSubscribeResponse(status=send_status, email=email)


def collect_weekly_publication_items(
    *,
    now: datetime | None = None,
    window_days: int = 7,
) -> tuple[datetime, list[dict[str, str]]]:
    """Build the shared weekly brief once from public regulatory publications."""
    moment = now or datetime.utcnow()
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    window_start = moment - timedelta(days=window_days)
    items: list[dict[str, str]] = []
    with platform_session_scope(commit=False) as db:
        use_shared_search_path(db)
        publications = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.source.in_(NEWSLETTER_SOURCES))
            .filter(
                or_(
                    RegulatoryPublication.published_at >= window_start,
                    RegulatoryPublication.created_at >= window_start,
                )
            )
            .order_by(
                RegulatoryPublication.published_at.desc().nullslast(),
                RegulatoryPublication.created_at.desc(),
            )
            .all()
        )
        seen_urls: set[str] = set()
        seen_ids: set[str] = set()
        for publication in publications:
            effective = publication.published_at or publication.created_at
            if effective is not None and _as_naive_utc(effective) < window_start:
                continue
            item = _weekly_item_from_publication(publication, seen_urls)
            if item is None:
                continue
            items.append(item)
            if publication.id:
                seen_ids.add(publication.id)
        present = {item['source'] for item in items}
        for source in NEWSLETTER_SOURCES:
            if source in present:
                continue
            fallback = newest_publication_for_source(
                db,
                source,
                exclude_ids=seen_ids,
            )
            if fallback is None:
                continue
            item = _weekly_item_from_publication(fallback, seen_urls)
            if item is None:
                continue
            items.append(item)
            present.add(source)
    return moment, items


def _weekly_item_from_publication(
    publication: RegulatoryPublication,
    seen_urls: set[str],
) -> dict[str, str] | None:
    url = (publication.url or '').strip()
    if url and url in seen_urls:
        return None
    if url:
        seen_urls.add(url)
    title = publication.title or 'Untitled'
    summary = _newsletter_table_summary(publication)
    effective = publication.published_at or publication.created_at
    return {
        'source': (publication.source or '').strip().upper(),
        'title': title,
        'summary': summary,
        'kind': classify_publication_kind(title),
        'published_label': format_brief_date(effective),
        'url': url or '#',
        'comment_period': '1' if looks_like_comment_period(title, summary) else '0',
    }


def send_weekly_newsletter(
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> NewsletterSendResponse:
    """Generate the brief once and email every unique subscriber across orgs."""
    cfg = settings or get_settings()
    week_ending, items = collect_weekly_publication_items(now=now)
    origin = cfg.public_frontend_url.rstrip('/')
    html = build_weekly_newsletter_html(
        week_ending=week_ending,
        items=items,
        signup_url=f'{origin}/sign-up',
        read_more_url=f'{origin}/newsletter',
    )
    recipients, skipped_orgs = list_all_subscriber_emails()
    if not recipients:
        logger.info(
            'newsletter_weekly_skipped',
            reason='no_subscribers',
            publication_count=len(items),
            skipped_orgs=skipped_orgs,
        )
        return NewsletterSendResponse(
            status='skipped',
            publication_count=len(items),
            recipient_count=0,
            sent_count=0,
            skipped_orgs=skipped_orgs,
        )

    subject = (
        f'Reg-AI Newsletter: Weekly Regulatory Brief '
        f'[{week_ending.strftime("%b")} {week_ending.day} {week_ending.year}]'
    )
    sent_count = 0
    errors: list[str] = []
    for email in recipients:
        result = send_resend_email(to_email=email, subject=subject, html=html)
        send_status = result.get('status')
        if send_status == 'sent':
            sent_count += 1
            continue
        if send_status == 'skipped':
            continue
        reason = str(result.get('error') or result.get('reason') or 'send_failed')
        errors.append(f'{email}: {reason}'[:240])

    logger.info(
        'newsletter_weekly_completed',
        publication_count=len(items),
        recipient_count=len(recipients),
        sent_count=sent_count,
        skipped_orgs=skipped_orgs,
        error_count=len(errors),
    )
    return NewsletterSendResponse(
        status='completed',
        publication_count=len(items),
        recipient_count=len(recipients),
        sent_count=sent_count,
        skipped_orgs=skipped_orgs,
        errors=errors[:25],
    )


def list_all_subscriber_emails() -> tuple[list[str], int]:
    """Walk every org newsletter_subscribers table plus public signups."""
    emails: set[str] = set()
    skipped_orgs = 0
    with platform_session_scope(commit=False) as db:
        org_ids = [
            row
            for (row,) in db.query(Organization.auth_org_id)
            .order_by(Organization.auth_org_id.asc())
            .all()
            if row
        ]
        tenant_modes = {
            row.auth_org_id: row.isolation_mode
            for row in db.query(OrganizationTenant).all()
            if row.auth_org_id
        }
        public_signups = [
            (row.email or '').strip().lower()
            for row in db.query(GeneralNewsletterSubscriber).all()
        ]

    for org_id in org_ids:
        try:
            with session_scope(commit=False, org_id=org_id) as db:
                query = db.query(NewsletterSubscriber)
                if tenant_modes.get(org_id) != 'schema':
                    query = query.filter(NewsletterSubscriber.auth_org_id == org_id)
                rows = query.all()
                if not rows:
                    skipped_orgs += 1
                    continue
                for row in rows:
                    email = (row.email or '').strip().lower()
                    if email:
                        emails.add(email)
        except Exception:
            logger.exception('newsletter_subscriber_org_scan_failed', org_id=org_id)
            skipped_orgs += 1

    for email in public_signups:
        if email:
            emails.add(email)
    return sorted(emails), skipped_orgs


def _persist_subscriber(
    *,
    email: str,
    org_id: str | None,
    source: str,
    invited_by: str | None = None,
    list_kind: str | None = None,
) -> bool:
    """Insert a subscriber. Returns False when the address is already stored.

    Public /newsletter signups go to ``general_newsletter_subscribers``.
    Organization invites from /publications go to that tenant's
    ``newsletter_subscribers`` table.
    """
    kind = _normalize_list_kind(list_kind, org_id=org_id)
    scoped_org = _real_org_id(org_id)
    if kind == NEWSLETTER_LIST_ORG and not scoped_org:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='This organization invitation is missing its organization.',
        )
    if kind == NEWSLETTER_LIST_ORG and scoped_org:
        subscriber = NewsletterSubscriber(
            id=str(uuid4()),
            email=email,
            auth_org_id=scoped_org,
            invited_by=invited_by,
            source=source,
            subscribed_at=datetime.utcnow(),
        )
        with session_scope(org_id=scoped_org) as db:
            existing_org = (
                db.query(NewsletterSubscriber)
                .filter(NewsletterSubscriber.email == email)
                .filter(NewsletterSubscriber.auth_org_id == scoped_org)
                .first()
            )
            if existing_org:
                return False
            return _flush_subscriber(db, subscriber)

    general = GeneralNewsletterSubscriber(
        id=str(uuid4()),
        email=email,
        subscribed_at=datetime.utcnow(),
    )
    with platform_session_scope() as db:
        existing_general = (
            db.query(GeneralNewsletterSubscriber)
            .filter(GeneralNewsletterSubscriber.email == email)
            .first()
        )
        if existing_general:
            return False
        return _flush_subscriber(db, general)


def _flush_subscriber(
    db: Session,
    subscriber: NewsletterSubscriber | GeneralNewsletterSubscriber,
) -> bool:
    db.add(subscriber)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return False
    return True


def _organization_name(db: Session, org_id: str) -> str | None:
    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == org_id)
        .first()
    )
    if organization is None:
        return None
    name = (organization.name or '').strip()
    return name or None


def _token_secret_bytes(settings: Settings | None) -> bytes:
    cfg = settings or get_settings()
    return cfg.resolved_newsletter_token_secret.encode('utf-8')


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _newsletter_table_summary(publication: RegulatoryPublication) -> str:
    """Use the same SUMMARY text as the News Publications table."""
    # Force-load deferred text columns so publication_summary can see them.
    stored = (getattr(publication, 'feed_summary', None) or '').strip()
    _ = getattr(publication, 'raw_text', None)
    summary = (publication_summary(publication) or '').strip()
    title = publication.title or ''
    if summary and not publication_text_is_title_only(summary, title):
        return summary
    if stored and not publication_text_is_title_only(stored, title):
        return stored
    if summary:
        return summary
    return _PENDING_FEED_SUMMARY
