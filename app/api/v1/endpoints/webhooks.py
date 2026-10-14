"""Inbound webhooks (Stripe billing, Supabase Auth, deprecated legacy Clerk)."""

from __future__ import annotations

import json
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security_hardening import report_failed_authentication
from app.db.session import get_platform_db
from app.services.billing_service import handle_stripe_webhook, verify_stripe_webhook

logger = structlog.get_logger(__name__)
router = APIRouter(prefix='/webhooks', tags=['webhooks'])


@router.post('/stripe')
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias='stripe-signature'),
    db: Session | None = Depends(get_platform_db),
) -> dict[str, str]:
    """Receive Stripe billing lifecycle events.

    Configure in the Stripe Dashboard → Webhooks:
    - Endpoint: ``https://<api-host>/api/v1/webhooks/stripe``
    - Events: ``checkout.session.completed``, ``customer.subscription.*``
    - Secret → ``STRIPE_WEBHOOK_SECRET``
    """
    settings = get_settings()
    payload = await request.body()

    try:
        event = verify_stripe_webhook(payload, stripe_signature, settings)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_401_UNAUTHORIZED:
            report_failed_authentication(
                reason='invalid_stripe_webhook_signature',
                path=request.url.path,
                client_ip=request.client.host if request.client else 'unknown',
            )
        raise

    event_type = str(event.get('type') or '')
    logger.info('stripe_webhook_received', event_type=event_type)

    action = 'ignored'
    if db is None:
        logger.warning('stripe_webhook_sync_skipped', reason='database_unavailable')
    else:
        try:
            action = handle_stripe_webhook(db, event)
        except SQLAlchemyError:
            db.rollback()
            logger.exception('stripe_webhook_sync_failed', event_type=event_type)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail='Unable to apply the Stripe webhook event',
            ) from None

    return {'status': 'accepted', 'event_type': event_type or 'unknown', 'action': action}


@router.post('/supabase')
async def supabase_webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None, alias='x-webhook-secret'),
) -> dict[str, str]:
    """Optional stub for Supabase Auth webhook events (user lifecycle, etc.).

    Configure ``SUPABASE_WEBHOOK_SECRET`` and send it as the ``x-webhook-secret``
    header from Supabase's Database/Auth webhook config to enable this endpoint.
    """
    settings = get_settings()
    if not settings.supabase_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SUPABASE_WEBHOOK_SECRET is not configured',
        )
    if x_webhook_secret != settings.supabase_webhook_secret:
        report_failed_authentication(
            reason='invalid_supabase_webhook_secret',
            path=request.url.path,
            client_ip=request.client.host if request.client else 'unknown',
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid Supabase webhook secret',
        )

    payload = await request.body()
    try:
        body: dict[str, Any] = json.loads(payload.decode('utf-8')) if payload else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid JSON payload',
        ) from exc

    event_type = str(body.get('type') or body.get('table') or 'unknown')
    logger.info('supabase_webhook_received', event_type=event_type)
    return {'status': 'accepted', 'event_type': event_type}


@router.post('/clerk', deprecated=True)
async def clerk_webhook(request: Request) -> dict[str, str]:
    """Deprecated: Clerk has been replaced by Supabase Auth.

    Kept as a no-op so any still-configured Clerk webhook does not 404; it
    accepts the request without validating or applying anything.
    """
    await request.body()
    logger.info('clerk_webhook_deprecated_noop')
    return {
        'status': 'ignored',
        'reason': (
            'Clerk webhooks are deprecated; migrate to /webhooks/stripe or /webhooks/supabase'
        ),
    }
