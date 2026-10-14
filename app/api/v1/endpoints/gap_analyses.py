from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.member_capabilities import require_delete_resources
from app.core.roles import (
    CHIEF_COMPLIANCE_OFFICER,
    INTERNAL_ROLES,
    NOMINATED_TEAM_MEMBER,
)
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.agent import AgentRunResponse
from app.schemas.gap_analyses import GapAnalysisResponse
from app.services.gap_analysis_service import (
    delete_gap_analysis_for_org,
    get_gap_analysis_for_org,
    list_gap_analyses_for_org,
)
from app.services.regulatory_compliance_workflow_service import (
    count_active_internal_policies,
)
from app.workers.jobs import run_regulatory_sync_background

router = APIRouter(prefix='/gap-analyses', tags=['gap-analyses'])

GAP_ANALYSIS_RUNNER_ROLES = INTERNAL_ROLES | {
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
}


def require_gap_analysis_runner(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.has_any_role(GAP_ANALYSIS_RUNNER_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to re-run gap analysis',
        )
    return user


@router.get('', response_model=list[GapAnalysisResponse])
def get_gap_analyses(
    impact_score: Literal['HIGH', 'MEDIUM', 'INFO'] | None = None,
    trigger_type: Literal['publication', 'business_change', 'policy_update', 'marketing_content'] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    dedupe_by_policy: bool = Query(
        default=True,
        description=(
            'When true (default), return one row per Internal policy name '
            '(newest analysis wins across versions and trigger types). '
            'Set false to keep every matching gap — needed for publication impact mapping.'
        ),
    ),
    include_findings: bool = Query(
        default=True,
        description=(
            'When false, omit full findings/citations so list pages load faster. '
            'Fetch GET /gap-analyses/{id} when expanding a row.'
        ),
    ),
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[GapAnalysisResponse]:
    """List Internal policy documents that contain regulation gaps needing review."""
    return list_gap_analyses_for_org(
        db=db,
        org_id=user.org_id,
        impact_score=impact_score,
        trigger_type=trigger_type,
        limit=limit,
        dedupe_by_policy=dedupe_by_policy,
        include_findings=include_findings,
    )


@router.get('/{gap_analysis_id}', response_model=GapAnalysisResponse)
def get_gap_analysis(
    gap_analysis_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> GapAnalysisResponse:
    """Return one gap analysis including full sentence-level findings."""
    analysis = get_gap_analysis_for_org(db, user.org_id, gap_analysis_id)
    if analysis is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Gap analysis was not found',
        )
    return analysis


@router.delete('/{gap_analysis_id}', status_code=status.HTTP_204_NO_CONTENT)
def delete_gap_analysis(
    gap_analysis_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_db),
) -> None:
    """Force-delete a gap analysis row from Regulation Gap Analysis."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Gap analysis delete requires a configured database',
        )
    deleted = delete_gap_analysis_for_org(db, user.org_id, gap_analysis_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Gap analysis was not found',
        )


@router.post('/reanalyze-stale', response_model=AgentRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def reanalyze_stale_gap_analyses(
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_gap_analysis_runner),
    db: Session | None = Depends(get_db),
) -> AgentRunResponse:
    """Re-run scrape + Internal Policy gap analysis for the caller's organization.

    Matches ``POST /regulatory/sync``: scrapes SEC/FINRA/CFTC, refreshes reference
    embeddings, then re-reviews active Internal Policies. Formerly only rebuilt
    RULE_FALLBACK / LLM_FAILED rows, which looked like a no-op for most orgs.
    """
    run_id = str(uuid4())
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Gap analysis re-run requires a configured database',
        )
    if count_active_internal_policies(db, user.org_id) < 1:
        return AgentRunResponse(
            run_id=run_id,
            status='skipped',
            message=(
                'No active internal policies found for this organization. '
                'Upload an Internal Policy first — scraping and gap analysis '
                'have nothing to review against until then.'
            ),
        )

    background_tasks.add_task(run_regulatory_sync_background, user.org_id)
    return AgentRunResponse(
        run_id=run_id,
        status='accepted',
        message=(
            'AI analysis started: scraping government sources, updating reference '
            'policies, then reviewing Internal Policies for gaps. '
            'Refresh this page in 1–3 minutes to see updated results.'
        ),
    )
