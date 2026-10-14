from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.roles import REVIEW_ACCESS_ROLES, REVIEW_ACTION_ROLES
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.models.core import GapAnalysis, PolicyRevision
from app.schemas.policies import PolicyFileDownloadResponse
from app.schemas.review import (
    AssignReviewerRequest,
    AssignReviewerResponse,
    AuditLogResponse,
    GapSummaryResponse,
    PolicyAuditChainListResponse,
    PolicyRevisionResponse,
    ReviewDashboardResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewHistoryResponse,
)
from app.services.document_chunk_service import schedule_redline_similarity_backfills
from app.services.gap_analysis_service import (
    clear_pending_redline_url_for_gap,
    gap_needs_fresh_redline,
    persist_cleared_needs_review_decision,
)
from app.services.marketing_redline_service import (
    ensure_marketing_redline_for_revision,
    generate_marketing_redline_for_gap,
    revision_is_marketing,
)
from app.services.redline_generation_service import (
    ensure_pending_revision_redline,
    finalize_policy_redline_background,
    generate_policy_redline,
    policy_revision_response,
    schedule_pending_redline_generations,
)
from app.services.review_service import (
    assign_revision_reviewer,
    get_audit_logs,
    get_gap_summary,
    get_pending_revisions,
    get_policy_audit_chains,
    get_redline_download_content_with_regeneration,
    get_redline_download_url,
    get_review_history,
    record_review_decision,
)

router = APIRouter(prefix="/reviews", tags=["reviews"])
REDLINE_GENERATOR_ROLES = REVIEW_ACTION_ROLES


def require_redline_generator(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Ensure the current user has a role allowed to generate policy redlines."""
    if not user.has_any_role(REDLINE_GENERATOR_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role to generate a policy redline",
        )
    return user


def require_review_access(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.has_any_role(REVIEW_ACCESS_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role to view or action policy reviews",
        )
    return user


@router.get("/pending", response_model=ReviewDashboardResponse)
async def list_pending_reviews(
    background_tasks: BackgroundTasks,
    kick_jobs: bool = Query(
        default=False,
        description=(
            "When true, queue missing redline generation and similarity backfills. "
            "Keep false for dashboard reads to avoid repeated heavy background work."
        ),
    ),
    include_findings: bool = Query(
        default=True,
        description=(
            "When false, omit per-finding RULE/GAP/FIX arrays and similarity details "
            "for faster dashboard reads."
        ),
    ),
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> ReviewDashboardResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Review dashboard requires a configured database",
        )
    dashboard = await get_pending_revisions(
        user.org_id,
        db,
        include_findings=include_findings,
    )
    if kick_jobs:
        pending_payload = [
            {
                "revision_id": item.revision_id,
                "gap_analysis_id": item.gap_analysis_id,
                "redline_url": item.redline_url,
            }
            for item in dashboard.pending_revisions
        ]
        schedule_pending_redline_generations(
            background_tasks, user.org_id, pending_payload
        )
        schedule_redline_similarity_backfills(
            background_tasks,
            user.org_id,
            [
                item
                for item, revision in zip(
                    pending_payload, dashboard.pending_revisions, strict=True
                )
                if item["redline_url"]
                and revision.redline_internal_cosine_similarity is None
                and revision.redline_regulator_cosine_similarity is None
            ],
        )
    return dashboard


@router.get("/audit-chains", response_model=PolicyAuditChainListResponse)
def list_policy_audit_chains(
    policy_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> PolicyAuditChainListResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit trail requires a configured database",
        )
    return get_policy_audit_chains(
        org_id=user.org_id, db=db, policy_id=policy_id, limit=limit
    )


@router.get('/history', response_model=ReviewHistoryResponse)
def list_review_history(
    limit: int = Query(default=100, ge=1, le=500),
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> ReviewHistoryResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Review history requires a configured database',
        )
    return get_review_history(user.org_id, db, limit=limit)


@router.get("/gap-analyses/{gap_analysis_id}", response_model=GapSummaryResponse)
def read_gap_summary(
    gap_analysis_id: str,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> GapSummaryResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Review summary requires a configured database",
        )
    return get_gap_summary(gap_analysis_id, user.org_id, db)


@router.get("/{revision_id}/redline-download-url", response_model=PolicyFileDownloadResponse)
def read_redline_download_url(
    revision_id: str,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> PolicyFileDownloadResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redline downloads require a configured database",
        )
    return PolicyFileDownloadResponse(
        url=get_redline_download_url(revision_id, user.org_id, db),
        expires_in=900,
    )


@router.get("/{revision_id}/redline")
async def download_redline(
    revision_id: str,
    user: CurrentUser = Depends(require_review_access),
) -> Response:
    """Stream a redline DOCX through the API, regenerating when storage is stale."""
    content, filename, content_type = await get_redline_download_content_with_regeneration(
        revision_id,
        user.org_id,
    )
    return Response(
        content=content,
        media_type=content_type,
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@router.post(
    "/gap-analyses/{gap_analysis_id}/redline",
    response_model=PolicyRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generate_redline(
    gap_analysis_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_redline_generator),
    db: Session | None = Depends(get_db),
) -> PolicyRevisionResponse:
    needs_fresh_redline = False
    if db is not None:
        gap = (
            db.query(GapAnalysis)
            .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == user.org_id)
            .one_or_none()
        )
        if gap is not None and gap.trigger_type == 'marketing_content':
            return await generate_marketing_redline_for_gap(
                gap_analysis_id,
                user.org_id,
                db,
                background_tasks,
            )
        needs_fresh_redline = gap_needs_fresh_redline(gap)
        if needs_fresh_redline:
            clear_pending_redline_url_for_gap(
                db,
                org_id=user.org_id,
                gap_analysis_id=gap_analysis_id,
            )
    revision = await generate_policy_redline(
        gap_analysis_id,
        user,
        db,
        defer_generation=True,
        background_tasks=background_tasks,
    )
    if db is not None:
        refreshed_gap = (
            db.query(GapAnalysis)
            .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == user.org_id)
            .one_or_none()
        )
        persist_cleared_needs_review_decision(db, refreshed_gap)
    if not revision.redline_file_url or needs_fresh_redline:
        background_tasks.add_task(
            finalize_policy_redline_background,
            revision.id,
            gap_analysis_id,
            user.org_id,
        )
    return revision


@router.post(
    "/{revision_id}/generate-redline",
    response_model=PolicyRevisionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_revision_redline(
    revision_id: str,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> PolicyRevisionResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redline generation requires a configured database",
        )

    revision = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.id == revision_id,
            PolicyRevision.org_id == user.org_id,
            PolicyRevision.status == "PENDING",
        )
        .one_or_none()
    )
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
    if revision.redline_file_url:
        return policy_revision_response(revision)
    if revision_is_marketing(db, revision):
        return await ensure_marketing_redline_for_revision(revision.id, user.org_id, db)

    background_tasks.add_task(
        finalize_policy_redline_background,
        revision.id,
        revision.gap_analysis_id,
        user.org_id,
    )
    return policy_revision_response(revision)


@router.post(
    "/{revision_id}/generate-redline/sync",
    response_model=PolicyRevisionResponse,
)
async def generate_revision_redline_sync(
    revision_id: str,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> PolicyRevisionResponse:
    if db is not None:
        revision = (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.id == revision_id,
                PolicyRevision.org_id == user.org_id,
                PolicyRevision.status == 'PENDING',
            )
            .one_or_none()
        )
        if revision is not None and revision_is_marketing(db, revision):
            return await ensure_marketing_redline_for_revision(revision.id, user.org_id, db)
    return await ensure_pending_revision_redline(revision_id, user.org_id)


@router.post("/{revision_id}/assign-reviewer", response_model=AssignReviewerResponse)
async def assign_reviewer_to_revision(
    revision_id: str,
    payload: AssignReviewerRequest,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> AssignReviewerResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Reviewer assignment requires a configured database",
        )
    return await assign_revision_reviewer(revision_id, payload, user, db)


@router.post("/{revision_id}/decision", response_model=ReviewDecisionResponse)
async def decide_revision(
    revision_id: str,
    payload: ReviewDecisionRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_db),
) -> ReviewDecisionResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Review decisions require a configured database",
        )
    return await record_review_decision(
        revision_id,
        payload,
        user,
        db,
        background_tasks=background_tasks,
    )


@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_review_audit_logs(
    user: CurrentUser = Depends(require_review_access),
    revision_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session | None = Depends(get_db),
) -> list[AuditLogResponse]:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Review audit logs require a configured database",
        )
    return get_audit_logs(user.org_id, revision_id=revision_id, limit=limit, db=db)
