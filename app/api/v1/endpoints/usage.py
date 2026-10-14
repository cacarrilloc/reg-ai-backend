from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.usage import UsageIncrementRequest, UsageSummaryResponse
from app.services.usage_service import get_usage_summary, post_usage_increment

router = APIRouter(prefix='/usage', tags=['usage'])


@router.get('/summary', response_model=UsageSummaryResponse)
def get_summary(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> UsageSummaryResponse:
    return get_usage_summary(user, db)


@router.post('/increment', response_model=UsageSummaryResponse)
def post_increment(
    payload: UsageIncrementRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> UsageSummaryResponse:
    return post_usage_increment(payload, user, db)
