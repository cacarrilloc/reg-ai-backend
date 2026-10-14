from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.roles import (
    CHIEF_COMPLIANCE_OFFICER,
    INTERNAL_ROLES,
    MASTER_USER,
    NOMINATED_TEAM_MEMBER,
)
from app.core.security import CurrentUser, get_current_user, require_internal_role
from app.db.session import get_db
from app.schemas.regulatory import (
    RegulatoryIngestResponse,
    RegulatoryPublicationResponse,
    RegulatorySyncResponse,
)
from app.services.regulatory_compliance_workflow_service import (
    count_active_internal_policies,
)
from app.services.regulatory_monitoring_service import (
    ingest_regulatory_feeds,
    list_publications_for_user,
)
from app.workers.jobs import run_regulatory_sync_background

router = APIRouter(prefix='/regulatory', tags=['regulatory-monitoring'])

REGULATORY_SYNC_ROLES = INTERNAL_ROLES | {
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
}


def require_regulatory_sync_runner(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.has_any_role(REGULATORY_SYNC_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to sync regulatory sources',
        )
    return user


@router.get('/publications', response_model=list[RegulatoryPublicationResponse])
def get_publications(
    limit: int = Query(default=50, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[RegulatoryPublicationResponse]:
    return list_publications_for_user(
        db=db,
        org_id=user.org_id,
        is_internal_user=user.has_any_role(INTERNAL_ROLES),
        limit=limit,
    )


@router.post('/ingest', response_model=RegulatoryIngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def ingest_feeds(
    user: CurrentUser = Depends(
        require_internal_role(MASTER_USER)),
    db: Session | None = Depends(get_db),
) -> RegulatoryIngestResponse:
    del user, db
    # Do not pass the request session — scrape/embeds must not hold a pool connection.
    return await ingest_regulatory_feeds(org_id=None)


@router.post('/sync', response_model=RegulatorySyncResponse, status_code=status.HTTP_202_ACCEPTED)
async def sync_regulatory_sources(
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_regulatory_sync_runner),
    db: Session | None = Depends(get_db),
) -> RegulatorySyncResponse:
    """Queue a SEC/FINRA/CFTC scrape and Policy Library reference sync."""
    org_id = user.org_id
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Regulatory sync requires a configured database',
        )
    # The workflow skips orgs without Internal Policies, so report that up front
    # instead of promising a scrape that will not happen.
    if count_active_internal_policies(db, org_id) < 1:
        from app.services.marketing_regulation_sync import schedule_marketing_regulation_sync

        schedule_marketing_regulation_sync(org_id)
        return RegulatorySyncResponse(
            status='accepted',
            message=(
                'Scraping official SEC/FINRA marketing rules into System Regulations. '
                'Upload an internal policy to also refresh FINRA/SEC/CFTC notices and run gap analysis.'
            ),
        )

    background_tasks.add_task(run_regulatory_sync_background, org_id)
    return RegulatorySyncResponse(
        status='accepted',
        message=(
            'Regulatory compliance workflow started. Scraping '
            'https://www.sec.gov/newsroom, https://www.finra.org/media-center, and '
            'https://www.cftc.gov/PressRoom (plus RSS backups); then updating reference '
            'policies and reviewing internal policies for gaps. Refresh in about a minute.'
        ),
    )
