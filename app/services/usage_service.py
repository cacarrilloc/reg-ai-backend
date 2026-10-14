"""Daily per-org usage metering for billing/limits."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.models.core import UsageCounter
from app.schemas.usage import UsageCounterEntry, UsageIncrementRequest, UsageSummaryResponse

logger = structlog.get_logger(__name__)

DEFAULT_SUMMARY_WINDOW_DAYS = 30


def _today() -> date:
    return datetime.now(UTC).date()


def increment_usage(
    payload: UsageIncrementRequest,
    org_id: str,
    db: Session | None,
) -> None:
    """Best-effort increment; callers should not fail the primary action on error."""
    if db is None:
        return

    today = _today()
    row = (
        db.query(UsageCounter)
        .filter(
            UsageCounter.org_id == org_id,
            UsageCounter.day == today,
            UsageCounter.metric == payload.metric,
        )
        .one_or_none()
    )
    if row is None:
        row = UsageCounter(org_id=org_id, day=today, metric=payload.metric, count=payload.amount)
        db.add(row)
    else:
        row.count = (row.count or 0) + payload.amount

    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning(
            'usage_increment_failed',
            org_id=org_id,
            metric=payload.metric,
            error_type=type(exc).__name__,
        )


def record_usage(org_id: str, metric: str, amount: int = 1) -> None:
    """Fire-and-forget usage increment for internal callers (opens its own session)."""
    from app.db.session import session_scope

    try:
        with session_scope(commit=False, org_id=org_id) as db:
            increment_usage(UsageIncrementRequest(metric=metric, amount=amount), org_id, db)
    except RuntimeError:
        return


def get_usage_summary(
    user: CurrentUser,
    db: Session | None,
    *,
    days: int = DEFAULT_SUMMARY_WINDOW_DAYS,
) -> UsageSummaryResponse:
    if db is None:
        return UsageSummaryResponse(org_id=user.org_id, totals={}, daily=[])

    since = _today() - timedelta(days=max(1, days))
    rows = (
        db.query(UsageCounter)
        .filter(UsageCounter.org_id == user.org_id, UsageCounter.day >= since)
        .order_by(UsageCounter.day.desc())
        .all()
    )
    totals: dict[str, int] = defaultdict(int)
    daily: list[UsageCounterEntry] = []
    for row in rows:
        totals[row.metric] += row.count
        daily.append(UsageCounterEntry(day=row.day, metric=row.metric, count=row.count))

    return UsageSummaryResponse(org_id=user.org_id, totals=dict(totals), daily=daily)


def post_usage_increment(
    payload: UsageIncrementRequest,
    user: CurrentUser,
    db: Session | None,
) -> UsageSummaryResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Usage metering is unavailable without a configured database',
        )
    increment_usage(payload, user.org_id, db)
    return get_usage_summary(user, db)
