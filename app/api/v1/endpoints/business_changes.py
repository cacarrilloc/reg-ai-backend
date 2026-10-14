from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.member_capabilities import require_delete_resources
from app.core.roles import BUSINESS_CHANGE_CREATOR_ROLES
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.policies import (
    BusinessChangeAnalysisDownloadResponse,
    BusinessChangeCreate,
    BusinessChangeListItem,
    BusinessChangeResponse,
)
from app.services.business_change_service import (
    create_business_change,
    delete_business_change_for_org,
    get_business_change_analysis_download,
    get_business_change_for_org,
    list_business_changes_for_org,
    trigger_business_change_analysis_generation,
)

router = APIRouter(prefix='/business-changes', tags=['business-changes'])


def require_business_change_creator(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.has_any_role(BUSINESS_CHANGE_CREATOR_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to log a business change',
        )
    return user


@router.get('', response_model=list[BusinessChangeListItem])
def list_changes(
    limit: int = Query(default=50, ge=1, le=100),
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[BusinessChangeListItem]:
    return list_business_changes_for_org(db, user.org_id, limit)


@router.post('/replay-workflows', status_code=status.HTTP_202_ACCEPTED)
async def replay_workflows(
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_business_change_creator),
) -> dict[str, object]:
    """Re-queue analysis document generation for business changes without a ready report."""
    from app.services.workflow_dispatch_service import replay_business_change_workflows

    responses = await replay_business_change_workflows(
        user.org_id,
        background_tasks=background_tasks,
    )
    return {
        'replayed': len(responses),
        'statuses': [response.status for response in responses],
    }


@router.delete('/{change_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_change(
    change_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_db),
) -> None:
    """Remove a business change from history and any linked Internal Policy Gaps."""
    deleted = delete_business_change_for_org(db, user.org_id, change_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Business change not found',
        )


@router.get('/{change_id}', response_model=BusinessChangeListItem)
def get_change(
    change_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> BusinessChangeListItem:
    return get_business_change_for_org(db, user.org_id, change_id)


@router.get('/{change_id}/analysis', response_model=BusinessChangeAnalysisDownloadResponse)
def download_analysis(
    change_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> BusinessChangeAnalysisDownloadResponse:
    return get_business_change_analysis_download(db, user.org_id, change_id)


@router.post('/{change_id}/generate-analysis', status_code=status.HTTP_202_ACCEPTED)
async def generate_analysis(
    change_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> dict[str, object]:
    """Generate the AI regulatory analysis document for one business change."""
    return trigger_business_change_analysis_generation(
        db,
        user.org_id,
        change_id,
        background_tasks,
    )


@router.post('', response_model=BusinessChangeResponse, status_code=status.HTTP_201_CREATED)
async def create_change(
    payload: BusinessChangeCreate,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_business_change_creator),
    db: Session | None = Depends(get_db),
) -> BusinessChangeResponse:
    return await create_business_change(payload, user, db, background_tasks)
