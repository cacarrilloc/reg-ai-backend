"""RAG workflow dispatch for policy uploads and business changes."""

from __future__ import annotations

import structlog

from app.core.config import Settings, get_settings
from app.schemas.agent import AgentRunRequest
from app.services.internal_policy_workflow_service import process_all_internal_policies_for_org

logger = structlog.get_logger(__name__)


def workflow_agent_request(
    *,
    trigger_type: str,
    trigger_id: str,
    org_id: str,
    policy_ids: list[str] | None = None,
) -> AgentRunRequest:
    """Build a fresh RAG agent request that always re-runs gap analysis."""
    return AgentRunRequest(
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        org_id=org_id,
        policy_ids=policy_ids,
        force_reanalysis=True,
    )


def should_run_background_workflow(settings: Settings, inngest_delivered: bool) -> bool:
    """Run a local fallback only when Inngest did not accept the event."""
    del settings
    return not inngest_delivered


def queue_background_task(
    background_tasks: object | None,
    task: object,
    *args: object,
) -> bool:
    if background_tasks is None:
        return False
    add_task = getattr(background_tasks, 'add_task', None)
    if not callable(add_task):
        return False
    add_task(task, *args)
    return True


async def run_policy_upload_workflow(
    policy_id: str,
    org_id: str,
    *,
    skip_pipeline: bool = False,
) -> dict[str, object]:
    """Run gap analysis for one newly uploaded Internal Policy.

    Every upload live-scrapes FINRA/SEC/CFTC, embeds those regulation docs and
    the uploaded policy, then persists the Policy Update gap. Redline generation
    later compares the draft against those same regulation embeddings.
    Persist rules match cron: overwrite a same-name Policy Update gap (with a
    re-review prompt) or insert one beside any Business Change row. Request Review
    later updates the CCO queue and starts a new redline. Never auto-queues CCO review.
    """
    settings = get_settings()
    logger.info(
        'policy_upload_workflow_started',
        org_id=org_id,
        policy_id=policy_id,
        skip_pipeline=skip_pipeline,
    )
    if skip_pipeline:
        logger.info(
            'policy_upload_workflow_skipped_upload_pending_clear',
            org_id=org_id,
            policy_id=policy_id,
        )
        return {
            'status': 'completed',
            'policies_analyzed': 0,
            'scraped_sources': [],
            'embedded_chunks': 0,
            'reference_policies_updated': 0,
            'redlines_generated': 0,
            'gap_analysis_runs': 0,
            'message': (
                'Upload cleared UPLOAD_PENDING; skipped scrape, embed, and gap analysis.'
            ),
        }

    result = await process_all_internal_policies_for_org(
        org_id,
        settings=settings,
        policy_ids=[policy_id],
        skip_scrape=False,
    )
    logger.info(
        'policy_upload_workflow_completed',
        org_id=org_id,
        policy_id=policy_id,
        scraped_sources=result.get('scraped_sources'),
        embedded_chunks=result.get('embedded_chunks'),
        reference_policies_updated=result.get('reference_policies_updated'),
        redlines_generated=result.get('redlines_generated'),
        gap_analysis_runs=result.get('gap_analysis_runs'),
    )
    return {
        'status': 'completed',
        'policies_analyzed': 1,
        'scraped_sources': result.get('scraped_sources', []),
        'embedded_chunks': result.get('embedded_chunks', 0),
        'reference_policies_updated': result.get('reference_policies_updated', 0),
        'redlines_generated': result.get('redlines_generated', 0),
        'gap_analysis_runs': result.get('gap_analysis_runs', 0),
        'message': (
            f'Scraped FINRA/SEC/CFTC, refreshed regulation embeddings, and '
            f'analyzed the uploaded policy (gaps={result.get("gap_analysis_runs", 0)}).'
        ),
    }


async def run_business_change_workflow(
    change_id: str,
    org_id: str,
):
    """Match Internal Policies and open Gap Analysis, then write the History DOCX."""
    from app.db.session import reset_tenant_org_id, set_tenant_org_id
    from app.schemas.agent import AgentRunResponse
    from app.services.business_change_analysis_service import (
        ANALYSIS_STATUS_FAILED,
        ANALYSIS_STATUS_READY,
        generate_business_change_analysis_document,
        mark_business_change_analysis_status,
    )
    from app.services.business_change_policy_workflow_service import (
        process_business_change_policy_impacts,
    )

    token = set_tenant_org_id(org_id)
    try:
        # Persist Internal Policy Gaps first so Compliance Gaps updates even if the
        # History DOCX run is slow or times out on the LLM draft.
        policy_result = await process_business_change_policy_impacts(change_id, org_id)
        analysis_response = await generate_business_change_analysis_document(change_id, org_id)

        analysis_run_status = getattr(analysis_response, 'status', 'completed')
        if analysis_run_status in {'failed', 'skipped'}:
            mark_business_change_analysis_status(change_id, org_id, ANALYSIS_STATUS_FAILED)
        else:
            mark_business_change_analysis_status(change_id, org_id, ANALYSIS_STATUS_READY)

        message_parts = [
            str(getattr(analysis_response, 'message', '') or '').strip(),
        ]
        if policy_result.match.matched:
            gap_status = getattr(policy_result.gap_analysis, 'status', None)
            gap_message = getattr(policy_result.gap_analysis, 'message', None)
            if gap_status in {None, 'skipped', 'already_processed'}:
                message_parts.append(
                    f'Matched {len(policy_result.match.matched_policy_ids)} Internal Policy document(s), '
                    f'but Gap Analysis did not persist new findings'
                    + (f' ({gap_message}).' if gap_message else '.')
                )
            else:
                message_parts.append(
                    f'Matched {len(policy_result.match.matched_policy_ids)} Internal Policy document(s) '
                    'and opened Regulation Gap Analysis findings.'
                )
        elif policy_result.new_policy_id:
            message_parts.append(
                'No Internal Policy match found; created a new draft policy for Gap Analysis review.'
            )
        else:
            message_parts.append(policy_result.match.rationale)

        return AgentRunResponse(
            run_id=getattr(analysis_response, 'run_id', change_id),
            status=analysis_run_status,
            message=' '.join(part for part in message_parts if part),
        )
    except Exception:
        mark_business_change_analysis_status(change_id, org_id, ANALYSIS_STATUS_FAILED)
        raise
    finally:
        reset_tenant_org_id(token)


async def run_policy_upload_workflow_background(
    policy_id: str,
    org_id: str,
    skip_pipeline: bool = False,
) -> None:
    """Run the policy upload workflow outside the HTTP request lifecycle."""
    try:
        await run_policy_upload_workflow(policy_id, org_id, skip_pipeline=skip_pipeline)
    except Exception as exc:
        logger.exception(
            'policy_upload_workflow_background_failed',
            policy_id=policy_id,
            org_id=org_id,
            skip_pipeline=skip_pipeline,
            error_type=type(exc).__name__,
        )


async def run_business_change_workflow_background(change_id: str, org_id: str) -> None:
    """Run the business-change workflow outside the HTTP request lifecycle."""
    try:
        await run_business_change_workflow(change_id, org_id)
    except Exception as exc:
        logger.exception(
            'business_change_workflow_background_failed',
            change_id=change_id,
            org_id=org_id,
            error_type=type(exc).__name__,
        )


async def replay_business_change_workflows(
    org_id: str,
    *,
    limit: int = 25,
    background_tasks: object | None = None,
) -> list:
    """Re-queue analysis generation for business changes without a ready report."""
    from app.db.session import SessionLocal, session_scope
    from app.schemas.agent import AgentRunResponse

    if SessionLocal is None:
        logger.warning(
            'business_change_workflow_replay_skipped',
            org_id=org_id,
            reason='database_not_configured',
        )
        return []

    from app.models.core import BusinessChange
    from app.services.business_change_analysis_service import (
        ANALYSIS_STATUS_GENERATING,
        ANALYSIS_STATUS_READY,
    )

    with session_scope(org_id=org_id) as db:
        pending_changes = (
            db.query(BusinessChange)
            .filter(
                BusinessChange.org_id == org_id,
                BusinessChange.analysis_status != ANALYSIS_STATUS_READY,
            )
            .order_by(BusinessChange.flagged_at.desc())
            .limit(limit)
            .all()
        )
        pending_ids = [change.id for change in pending_changes]
        for change in pending_changes:
            change.analysis_status = ANALYSIS_STATUS_GENERATING

    if background_tasks is not None:
        queued: list[AgentRunResponse] = []
        for change_id in pending_ids:
            if queue_background_task(
                background_tasks,
                run_business_change_workflow_background,
                change_id,
                org_id,
            ):
                queued.append(
                    AgentRunResponse(
                        run_id=change_id,
                        status='queued',
                        message='Business change analysis generation queued.',
                    )
                )
        logger.info(
            'business_change_workflow_replay_queued',
            org_id=org_id,
            replayed=len(queued),
        )
        return queued

    responses = []
    for change_id in pending_ids:
        responses.append(await run_business_change_workflow(change_id, org_id))
    logger.info(
        'business_change_workflow_replay_completed',
        org_id=org_id,
        replayed=len(responses),
    )
    return responses
