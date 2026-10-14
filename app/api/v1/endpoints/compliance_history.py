from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.compliance_history import ComplianceHistoryListResponse
from app.services.compliance_history_service import (
    ALLOWED_HISTORY_WINDOWS,
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    list_compliance_history_for_org,
)

router = APIRouter(prefix='/compliance-history', tags=['compliance-history'])

HistoryTriggerType = Literal[
    'publication',
    'business_change',
    'policy_update',
    'marketing_content',
]


@router.get('', response_model=ComplianceHistoryListResponse)
def get_compliance_history(
    days: int = Query(default=30, description='Lookback window in days (7, 30, or 90)'),
    limit: int = Query(default=DEFAULT_HISTORY_LIMIT, ge=1, le=MAX_HISTORY_LIMIT),
    trigger_type: HistoryTriggerType | None = Query(
        default=None,
        description='When set, return only incidents with this trigger type.',
    ),
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> ComplianceHistoryListResponse:
    """List gap-incident metadata for the Compliance History dashboard table."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Compliance history requires a configured database',
        )
    if days not in ALLOWED_HISTORY_WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f'days must be one of {sorted(ALLOWED_HISTORY_WINDOWS)}',
        )
    return list_compliance_history_for_org(
        db,
        user.org_id,
        days=days,
        limit=limit,
        trigger_type=trigger_type,
    )
