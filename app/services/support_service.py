"""Customer support tickets raised from the org workspace."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import CurrentUser
from app.db.ordering import desc
from app.models.core import SupportTicket
from app.schemas.support import (
    SupportTicketCreate,
    SupportTicketListResponse,
    SupportTicketResponse,
    SupportTicketStatusUpdate,
)
from app.services.notification_service import html_text, send_resend_email

logger = structlog.get_logger(__name__)

DEFAULT_SUPPORT_NOTIFY_EMAIL = 'haider.mumtaz@ipxenterprises.com'


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _sender_from_user(user: CurrentUser) -> str:
    claims: dict[str, Any] = user.claims or {}
    for key in ('email', 'user_email', 'preferred_username'):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()

    app_metadata = claims.get('app_metadata')
    if isinstance(app_metadata, dict):
        email = app_metadata.get('email')
        if isinstance(email, str) and email.strip():
            return email.strip().lower()

    user_metadata = claims.get('user_metadata')
    if isinstance(user_metadata, dict):
        email = user_metadata.get('email')
        if isinstance(email, str) and email.strip():
            return email.strip().lower()

    return user.user_id or 'unknown'


def create_support_ticket(
    payload: SupportTicketCreate,
    user: CurrentUser,
    db: Session | None,
) -> SupportTicketResponse:
    ticket_id = str(uuid4())
    now = _now()
    description = (payload.description or '').strip()
    sender = _sender_from_user(user)

    if db is None:
        ticket = SupportTicket(
            id=ticket_id,
            org_id=user.org_id,
            created_by=user.user_id,
            sender=sender,
            subject=payload.subject,
            description=description,
            priority=payload.priority,
            status='open',
            created_at=now,
            updated_at=now,
        )
        _notify_support_team(ticket)
        return _response(ticket)

    record = SupportTicket(
        id=ticket_id,
        org_id=user.org_id,
        created_by=user.user_id,
        sender=sender,
        subject=payload.subject,
        description=description,
        priority=payload.priority,
        status='open',
        created_at=now,
        updated_at=now,
    )
    try:
        db.add(record)
        db.commit()
        db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to create the support ticket',
        ) from exc

    _notify_support_team(record)
    return _response(record)


def list_support_tickets(user: CurrentUser, db: Session | None) -> SupportTicketListResponse:
    if db is None:
        return SupportTicketListResponse(items=[], total=0)

    rows = (
        db.query(SupportTicket)
        .filter(SupportTicket.org_id == user.org_id)
        .order_by(desc(SupportTicket.created_at))
        .all()
    )
    items = [_response(row) for row in rows]
    return SupportTicketListResponse(items=items, total=len(items))


def update_support_ticket_status(
    ticket_id: str,
    payload: SupportTicketStatusUpdate,
    user: CurrentUser,
    db: Session | None,
) -> SupportTicketResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Support tickets are unavailable without a configured database',
        )

    ticket = (
        db.query(SupportTicket)
        .filter(SupportTicket.id == ticket_id, SupportTicket.org_id == user.org_id)
        .one_or_none()
    )
    if ticket is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Support ticket not found')

    ticket.status = payload.status
    ticket.updated_at = _now()
    try:
        db.commit()
        db.refresh(ticket)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update the support ticket',
        ) from exc

    return _response(ticket)


def _notify_support_team(ticket: SupportTicket) -> None:
    settings = get_settings()
    to_email = (settings.support_notify_email or DEFAULT_SUPPORT_NOTIFY_EMAIL).strip()
    if not to_email:
        to_email = DEFAULT_SUPPORT_NOTIFY_EMAIL

    created_day = ticket.created_at.date().isoformat() if ticket.created_at else '—'
    status_label = (ticket.status or 'open').strip().capitalize()
    if status_label.lower() == 'closed':
        status_label = 'Closed'
    elif status_label.lower() == 'escalated':
        status_label = 'Escalated'
    else:
        status_label = 'Open'

    priority = (ticket.priority or 'NORMAL').upper()
    badge_class = {
        'URGENT': 'high',
        'HIGH': 'high',
        'NORMAL': 'medium',
        'LOW': 'low',
    }.get(priority, 'info')

    safe_id = html_text(ticket.id)
    safe_subject = html_text(ticket.subject)
    safe_description = html_text(ticket.description).replace('\n', '<br />')
    safe_sender = html_text(ticket.sender)
    safe_org = html_text(ticket.org_id)
    safe_priority = html_text(priority)
    safe_status = html_text(status_label)
    safe_day = html_text(created_day)

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #0f172a; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: #f1f5f9; padding: 20px; }}
        .footer {{
            background-color: #1e293b; color: white; padding: 15px;
            text-align: center; border-radius: 0 0 8px 8px;
        }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: bold; }}
        .badge-high {{ background-color: #ef4444; color: white; }}
        .badge-medium {{ background-color: #f59e0b; color: white; }}
        .badge-low {{ background-color: #10b981; color: white; }}
        .badge-info {{ background-color: #64748b; color: white; }}
        .meta {{ background-color: white; border-radius: 6px; padding: 14px 16px; margin: 16px 0; }}
        .meta p {{ margin: 0 0 8px; }}
        .meta p:last-child {{ margin-bottom: 0; }}
        .description {{
            background-color: white; border-radius: 6px; padding: 14px 16px;
            margin-top: 8px; white-space: normal;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>New Support Ticket</h1>
        </div>
        <div class="content">
            <p>A new support ticket was submitted in Reg-AI.</p>
            <div class="meta">
                <p><strong>Priority:</strong> <span class="badge badge-{badge_class}">{safe_priority}</span></p>
                <p><strong>Ticket ID:</strong> {safe_id}</p>
                <p><strong>Subject:</strong> {safe_subject}</p>
                <p><strong>Sender:</strong> {safe_sender}</p>
                <p><strong>Organization:</strong> {safe_org}</p>
                <p><strong>Creation day:</strong> {safe_day}</p>
                <p><strong>Status:</strong> {safe_status}</p>
            </div>
            <p><strong>Description:</strong></p>
            <div class="description">{safe_description}</div>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    try:
        result = send_resend_email(
            to_email=to_email,
            subject=f'[{priority}] Support ticket: {ticket.subject}',
            html=email_html,
        )
        if result.get('status') == 'failed':
            logger.warning(
                'support_ticket_notification_failed',
                ticket_id=ticket.id,
                error=result.get('error'),
            )
    except Exception:  # noqa: BLE001 — notification failures must not block ticket creation
        logger.warning('support_ticket_notification_failed', ticket_id=ticket.id)


def _response(ticket: SupportTicket) -> SupportTicketResponse:
    description = getattr(ticket, 'description', None) or getattr(ticket, 'body', '') or ''
    sender = getattr(ticket, 'sender', None) or ticket.created_by
    priority = getattr(ticket, 'priority', None) or 'NORMAL'
    return SupportTicketResponse(
        id=ticket.id,
        org_id=ticket.org_id,
        created_by=ticket.created_by,
        sender=sender,
        subject=ticket.subject,
        description=description,
        message=description,
        body=description,
        priority=priority,
        status=ticket.status,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )
