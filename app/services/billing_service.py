"""Stripe billing: checkout/portal sessions, subscription sync, webhook handling."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.models.core import BillingCustomer, Subscription
from app.schemas.billing import (
    BillingStatusResponse,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    PortalSessionRequest,
    PortalSessionResponse,
)

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _stripe_client(settings: Settings):
    if not settings.stripe_secret_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='STRIPE_SECRET_KEY is not configured',
        )
    import stripe

    stripe.api_key = settings.stripe_secret_key
    return stripe


def get_or_create_customer(
    db: Session,
    *,
    auth_org_id: str,
    email: str | None = None,
    settings: Settings | None = None,
) -> BillingCustomer:
    """Return the local billing customer row, creating a Stripe customer if missing."""
    settings = settings or get_settings()
    existing = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if existing is not None:
        return existing

    stripe = _stripe_client(settings)
    try:
        customer = stripe.Customer.create(
            email=email or None,
            metadata={'auth_org_id': auth_org_id},
        )
    except Exception as exc:
        logger.exception('stripe_customer_create_failed', auth_org_id=auth_org_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to create a Stripe customer',
        ) from exc

    row = BillingCustomer(
        auth_org_id=auth_org_id,
        stripe_customer_id=customer['id'],
        created_at=_now(),
    )
    db.add(row)
    db.flush([row])
    return row


def create_checkout_session(
    payload: CheckoutSessionRequest,
    user: CurrentUser,
    db: Session | None,
) -> CheckoutSessionResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Billing is unavailable without a configured database',
        )
    settings = get_settings()
    price_id = payload.price_id or _resolve_price_id(payload.plan_code, settings)
    if not price_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='A price_id or a known plan_code is required',
        )

    stripe = _stripe_client(settings)
    customer = get_or_create_customer(db, auth_org_id=user.org_id, email=None, settings=settings)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to persist the billing customer',
        ) from exc

    try:
        session = stripe.checkout.Session.create(
            customer=customer.stripe_customer_id,
            mode='subscription',
            line_items=[{'price': price_id, 'quantity': 1}],
            success_url=payload.success_url,
            cancel_url=payload.cancel_url,
            metadata={'auth_org_id': user.org_id},
            subscription_data={'metadata': {'auth_org_id': user.org_id}},
        )
    except Exception as exc:
        logger.exception('stripe_checkout_session_failed', auth_org_id=user.org_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to create a Stripe Checkout session',
        ) from exc

    return CheckoutSessionResponse(checkout_url=session['url'], session_id=session['id'])


def create_portal_session(
    payload: PortalSessionRequest,
    user: CurrentUser,
    db: Session | None,
) -> PortalSessionResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Billing is unavailable without a configured database',
        )
    settings = get_settings()
    customer = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.auth_org_id == user.org_id)
        .one_or_none()
    )
    if customer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='No Stripe customer on file for this organization yet',
        )

    stripe = _stripe_client(settings)
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer.stripe_customer_id,
            return_url=payload.return_url,
        )
    except Exception as exc:
        logger.exception('stripe_portal_session_failed', auth_org_id=user.org_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to create a Stripe billing portal session',
        ) from exc

    return PortalSessionResponse(portal_url=session['url'])


def get_billing_status(user: CurrentUser, db: Session | None) -> BillingStatusResponse:
    if db is None:
        return BillingStatusResponse(auth_org_id=user.org_id, has_customer=False)

    customer = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.auth_org_id == user.org_id)
        .one_or_none()
    )
    subscription = (
        db.query(Subscription)
        .filter(Subscription.auth_org_id == user.org_id)
        .one_or_none()
    )
    if subscription is None:
        return BillingStatusResponse(auth_org_id=user.org_id, has_customer=customer is not None)

    return BillingStatusResponse(
        auth_org_id=user.org_id,
        has_customer=customer is not None,
        plan_code=subscription.plan_code,
        status=subscription.status,
        current_period_end=subscription.current_period_end,
        cancel_at_period_end=subscription.cancel_at_period_end,
    )


def _resolve_price_id(plan_code: str | None, settings: Settings) -> str | None:
    if not plan_code:
        return None
    normalized = plan_code.strip().lower()
    if normalized in {'monthly', 'month'}:
        return settings.stripe_price_monthly
    if normalized in {'annual', 'year', 'yearly'}:
        return settings.stripe_price_annual
    return None


def verify_stripe_webhook(payload: bytes, sig_header: str | None, settings: Settings) -> dict[str, Any]:
    """Verify a Stripe webhook payload and return the parsed event."""
    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='STRIPE_WEBHOOK_SECRET is not configured',
        )
    import stripe

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header or '', settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid Stripe webhook signature',
        ) from exc
    return event


def handle_stripe_webhook(db: Session, event: dict[str, Any]) -> str:
    """Apply a verified Stripe event to billing_customers/subscriptions. Returns action taken."""
    event_type = str(event.get('type') or '')
    data = _as_dict(event.get('data'))
    obj = _as_dict(data.get('object'))

    if event_type == 'checkout.session.completed':
        auth_org_id = _string(_as_dict(obj.get('metadata')).get('auth_org_id'))
        customer_id = _string(obj.get('customer'))
        subscription_id = _string(obj.get('subscription'))
        if not auth_org_id or not customer_id:
            return 'ignored'
        _upsert_billing_customer(db, auth_org_id=auth_org_id, stripe_customer_id=customer_id)
        if subscription_id:
            _upsert_subscription_stub(db, auth_org_id=auth_org_id, stripe_subscription_id=subscription_id)
        db.commit()
        return 'checkout_completed'

    if event_type in {
        'customer.subscription.created',
        'customer.subscription.updated',
        'customer.subscription.deleted',
    }:
        resolved_org_id: str | None = (
            _string(_as_dict(obj.get('metadata')).get('auth_org_id')) or None
        )
        if not resolved_org_id:
            customer_id = _string(obj.get('customer'))
            resolved_org_id = _org_id_for_stripe_customer(db, customer_id)
        if not resolved_org_id:
            return 'ignored'
        _apply_subscription_event(db, auth_org_id=resolved_org_id, subscription=obj)
        db.commit()
        return 'subscription_synced'

    return 'ignored'


def _org_id_for_stripe_customer(db: Session, stripe_customer_id: str) -> str | None:
    if not stripe_customer_id:
        return None
    row = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.stripe_customer_id == stripe_customer_id)
        .one_or_none()
    )
    return row.auth_org_id if row is not None else None


def _upsert_billing_customer(db: Session, *, auth_org_id: str, stripe_customer_id: str) -> BillingCustomer:
    row = (
        db.query(BillingCustomer)
        .filter(BillingCustomer.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if row is None:
        row = BillingCustomer(
            auth_org_id=auth_org_id,
            stripe_customer_id=stripe_customer_id,
            created_at=_now(),
        )
        db.add(row)
    elif row.stripe_customer_id != stripe_customer_id:
        row.stripe_customer_id = stripe_customer_id
    return row


def _upsert_subscription_stub(db: Session, *, auth_org_id: str, stripe_subscription_id: str) -> Subscription:
    row = (
        db.query(Subscription)
        .filter(Subscription.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if row is None:
        row = Subscription(
            auth_org_id=auth_org_id,
            stripe_subscription_id=stripe_subscription_id,
            status='incomplete',
            updated_at=_now(),
        )
        db.add(row)
    else:
        row.stripe_subscription_id = stripe_subscription_id
        row.updated_at = _now()
    return row


def _apply_subscription_event(db: Session, *, auth_org_id: str, subscription: dict[str, Any]) -> Subscription:
    row = (
        db.query(Subscription)
        .filter(Subscription.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if row is None:
        row = Subscription(auth_org_id=auth_org_id)
        db.add(row)

    items = _as_dict(subscription.get('items')).get('data') if isinstance(subscription.get('items'), dict) else None
    price_id = None
    if isinstance(items, list) and items:
        price_id = _string(_as_dict(_as_dict(items[0]).get('price')).get('id')) or None

    row.stripe_subscription_id = _string(subscription.get('id')) or row.stripe_subscription_id
    row.stripe_price_id = price_id or row.stripe_price_id
    row.status = _string(subscription.get('status')) or row.status
    row.cancel_at_period_end = bool(subscription.get('cancel_at_period_end') or False)
    period_end = subscription.get('current_period_end')
    if isinstance(period_end, (int, float)):
        row.current_period_end = datetime.fromtimestamp(period_end, tz=UTC).replace(tzinfo=None)
    row.updated_at = _now()
    return row


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ''
