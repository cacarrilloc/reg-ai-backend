from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.analytics import AnalyticsDashboardResponse, AnalyticsKpis
from app.services.analytics_service import get_analytics_dashboard, get_dashboard_kpis

router = APIRouter(prefix='/analytics', tags=['analytics'])


@router.get('/kpis', response_model=AnalyticsKpis)
def get_dashboard_kpi_summary(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> AnalyticsKpis:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Analytics requires a configured database',
        )
    return get_dashboard_kpis(db, user.org_id)


@router.get('/dashboard', response_model=AnalyticsDashboardResponse)
def get_dashboard_analytics(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> AnalyticsDashboardResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Analytics requires a configured database',
        )
    return get_analytics_dashboard(db, user.org_id)
