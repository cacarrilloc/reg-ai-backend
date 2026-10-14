from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.core.member_capabilities import require_delete_resources
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.policies import (
    ComplianceReportResponse,
    PolicyFileDownloadResponse,
    PolicyNameCheckResponse,
    PolicySummary,
    PolicyUploadResponse,
)
from app.services.compliance_report_service import generate_compliance_report
from app.services.policy_service import (
    check_policy_name,
    delete_internal_policy_for_org,
    get_policy_download_url,
    list_policies,
    list_policy_versions,
    queue_policy_upload,
)

router = APIRouter(prefix='/policies', tags=['policies'])


@router.get('', response_model=list[PolicySummary])
async def get_policies(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[PolicySummary]:
    return await list_policies(user.org_id, db)


@router.post('/compliance-report', response_model=ComplianceReportResponse, status_code=status.HTTP_201_CREATED)
async def create_compliance_report(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> ComplianceReportResponse:
    return await generate_compliance_report(user.org_id, db, raise_on_failure=True)


@router.post('/upload', response_model=PolicyUploadResponse, status_code=202)
async def upload_policy(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    policy_name: str = Form(...),
    user: CurrentUser = Depends(get_current_user),
) -> PolicyUploadResponse:
    return await queue_policy_upload(file, user, None, policy_name, background_tasks, persist=True)


@router.get('/name-check', response_model=PolicyNameCheckResponse)
async def get_policy_name_check(
    name: str = Query(..., min_length=1, max_length=180),
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> PolicyNameCheckResponse:
    return await check_policy_name(user.org_id, name, db)


@router.delete('/{policy_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_internal_policy(
    policy_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_db),
) -> None:
    """Remove an Internal Policy (all versions) and its gap/review rows."""
    deleted = delete_internal_policy_for_org(db, user.org_id, policy_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Internal policy not found',
        )


@router.get('/{policy_id}/versions', response_model=list[PolicySummary])
async def get_policy_versions(
    policy_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[PolicySummary]:
    return await list_policy_versions(policy_id, user.org_id, db)


@router.get('/{policy_id}/download-url', response_model=PolicyFileDownloadResponse)
async def get_policy_file_download_url(
    policy_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> PolicyFileDownloadResponse:
    return await get_policy_download_url(policy_id, user.org_id, db)
