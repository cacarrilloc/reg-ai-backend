"""Unified regulatory reference ingest and internal policy gap review workflow."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import (
    platform_session_scope,
    reset_tenant_org_id,
    session_scope,
    set_tenant_org_id,
)
from app.models.core import Organization, Policy
from app.schemas.regulatory import RegulatoryIngestResponse
from app.services.internal_policy_workflow_service import (
    process_all_internal_policies_for_org,
    refresh_org_government_reference_embeddings,
    scrape_all_regulator_websites,
    scrape_regulator_websites,
)
from app.services.policy_types import REGULATORY_REFERENCE_TYPE
from app.services.regulatory_reference_policy_service import (
    ALL_REFERENCE_SOURCES,
)

logger = structlog.get_logger(__name__)
_background_tasks: set[asyncio.Task[Any]] = set()
_workflow_lock = asyncio.Lock()


def schedule_regulatory_compliance_workflow(org_id: str | None = None) -> None:
    """Queue the org Internal Policy compliance loop without blocking the request."""
    task = asyncio.create_task(
        run_regulatory_compliance_workflow(org_id=org_id),
        name=f'regulatory-compliance-{org_id or "all"}',
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_background_task_result)


def _log_background_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.exception(
            'regulatory_compliance_background_task_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )


def count_active_internal_policies(db: Session, org_id: str | None = None) -> int:
    """Return how many ACTIVE Internal Policy docs exist (optionally for one org)."""
    query = db.query(Policy.id).filter(
        Policy.status == 'ACTIVE',
        Policy.type != REGULATORY_REFERENCE_TYPE,
    )
    if org_id:
        query = query.filter(Policy.org_id == org_id)
    return int(query.count())


def list_org_ids_with_internal_policies(db: Session, org_id: str | None = None) -> list[str]:
    """Orgs that have at least one ACTIVE Internal Policy (cron only processes these)."""
    query = (
        db.query(Policy.org_id)
        .filter(
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .distinct()
    )
    if org_id:
        query = query.filter(Policy.org_id == org_id)
    return [row for (row,) in query.all() if row]


def list_candidate_org_ids(db: Session, org_id: str | None = None) -> list[str]:
    """Platform-registry org ids to evaluate for Internal Policies."""
    if org_id:
        return [org_id]
    return [
        row
        for (row,) in db.query(Organization.auth_org_id).order_by(Organization.auth_org_id.asc()).all()
        if row
    ]


async def run_org_regulatory_scrape(
    org_id: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Scrape FINRA/SEC/CFTC into the org's database (tenant or platform fallback)."""
    del settings
    try:
        result = await scrape_all_regulator_websites(None, org_id=org_id)
        return {
            'status': 'completed',
            'fetched_count': result.fetched_count,
            'created_count': result.created_count,
            'errors': result.errors[:20],
            'scraped_sources': list(ALL_REFERENCE_SOURCES),
        }
    except Exception as exc:
        logger.exception(
            'org_regulatory_scrape_failed',
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        return {
            'status': 'failed',
            'error': str(exc)[:500],
            'scraped_sources': [],
        }


async def run_global_regulatory_scrape(
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Scrape into the platform DB (legacy shared path). Prefer ``run_org_regulatory_scrape``."""
    del settings
    try:
        result = await scrape_all_regulator_websites(None)
        return {
            'status': 'completed',
            'fetched_count': result.fetched_count,
            'created_count': result.created_count,
            'errors': result.errors[:20],
            'scraped_sources': list(ALL_REFERENCE_SOURCES),
        }
    except Exception as exc:
        logger.exception(
            'global_regulatory_scrape_failed',
            error_type=type(exc).__name__,
        )
        return {
            'status': 'failed',
            'error': str(exc)[:500],
            'scraped_sources': [],
        }


async def run_part1_regulatory_reference_ingest(
    db: Session,
    settings: Settings | None = None,
    *,
    org_id: str | None = None,
) -> RegulatoryIngestResponse | None:
    """Scrape FINRA, SEC, and CFTC and persist scraped government regulation content."""
    del settings, db
    sources = list(ALL_REFERENCE_SOURCES)
    if not sources:
        return None
    return await scrape_regulator_websites(None, sources, org_id=org_id)


def list_active_internal_policy_ids(db: Session, org_id: str) -> list[str]:
    return [
        policy_id
        for (policy_id,) in db.query(Policy.id)
        .filter(
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .order_by(Policy.name.asc())
        .all()
    ]


async def run_part2_internal_policy_gap_review(
    org_id: str,
    settings: Settings | None = None,
    *,
    skip_scrape: bool = True,
    reference_policies_updated: int = 0,
) -> dict[str, Any]:
    """For each Internal Policy: always run gap analysis against regulations.

    Cron path scrapes once globally, then refreshes org reference docs and calls this
    with ``skip_scrape=True``. Every active Internal Policy is analyzed and its gap
    row overwritten. ``reference_policies_updated`` is accepted for call-site
    compatibility and ignored for skip decisions.
    """
    del reference_policies_updated
    settings = settings or get_settings()
    return await process_all_internal_policies_for_org(
        org_id,
        settings=settings,
        skip_scrape=skip_scrape,
    )


async def run_regulatory_compliance_workflow(
    org_id: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Cron entrypoint: org-wide Internal Policy refresh.

    1. Live-scrape FINRA/SEC/CFTC and embed those regulation docs.
    2. Analyze every ACTIVE Internal Policy against the fresh vectors.
       a. Run gap analysis against the refreshed regulatory vectors.
       b. Overwrite an existing Policy Update Internal Policy Gaps row.
       c. Insert a Policy Update row when that policy has none.
       d. Mark overwritten rows so the user must Request Review again.
    Request Review (not this cron) updates the CCO Review Queue RULE/GAP/FIX
    and starts a new redline. Never auto-queues CCO review.
    """
    settings = settings or get_settings()

    async with _workflow_lock:
        try:
            with platform_session_scope(commit=False) as db:
                candidates = list_candidate_org_ids(db, org_id)

            org_ids: list[str] = []
            internal_policy_count = 0
            for candidate in candidates:
                with session_scope(org_id=candidate, commit=False) as db:
                    count = count_active_internal_policies(db, candidate)
                if count > 0:
                    org_ids.append(candidate)
                    internal_policy_count += count

            if internal_policy_count < 1 or not org_ids:
                reason = 'no_internal_policies'
                logger.info(
                    'regulatory_compliance_workflow_skipped',
                    org_id=org_id,
                    reason=reason,
                    internal_policy_count=internal_policy_count,
                )
                return {
                    'status': 'skipped',
                    'reason': reason,
                    'internal_policy_count': internal_policy_count,
                    'orgs_processed': 0,
                    'policies_analyzed': 0,
                    'gap_analysis_runs': 0,
                    'redlines_generated': 0,
                    'reference_policies_updated': 0,
                    'scraped_sources': [],
                    'part1': None,
                    'org_results': [],
                }

            org_results: list[dict[str, Any]] = []
            policies_analyzed = 0
            gap_runs = 0
            redlines_generated = 0
            reference_policies_updated = 0
            scraped_sources: set[str] = set()
            part1: dict[str, Any] | None = None

            for target_org_id in org_ids:
                token = set_tenant_org_id(target_org_id)
                try:
                    scrape_result = await run_org_regulatory_scrape(
                        target_org_id, settings
                    )
                    if part1 is None:
                        part1 = scrape_result
                    scraped_sources.update(scrape_result.get('scraped_sources') or [])

                    refs_updated = 0
                    try:
                        refs_updated = await refresh_org_government_reference_embeddings(
                            None,
                            org_id=target_org_id,
                            settings=settings,
                            sources=ALL_REFERENCE_SOURCES,
                        )
                    except Exception as exc:
                        logger.exception(
                            'org_reference_refresh_failed',
                            org_id=target_org_id,
                            error_type=type(exc).__name__,
                        )

                    org_result = await run_part2_internal_policy_gap_review(
                        target_org_id,
                        settings,
                        skip_scrape=True,
                        reference_policies_updated=refs_updated,
                    )
                    org_result['reference_policies_updated'] = max(
                        int(org_result.get('reference_policies_updated') or 0),
                        refs_updated,
                    )
                    org_result['scrape'] = scrape_result
                    org_results.append(org_result)
                    policies_analyzed += int(org_result.get('policies_processed') or 0)
                    gap_runs += int(org_result.get('gap_analysis_runs') or 0)
                    redlines_generated += int(org_result.get('redlines_generated') or 0)
                    reference_policies_updated += int(
                        org_result.get('reference_policies_updated') or 0
                    )
                    scraped_sources.update(org_result.get('scraped_sources') or [])
                    if int(scrape_result.get('created_count') or 0) > 0:
                        try:
                            from app.services.marketing_mode_b import run_mode_b_after_scrape

                            org_result['marketing_mode_b'] = await run_mode_b_after_scrape(
                                target_org_id
                            )
                        except Exception as exc:
                            logger.warning(
                                'marketing_mode_b_hook_failed',
                                org_id=target_org_id,
                                error_type=type(exc).__name__,
                            )
                            org_result['marketing_mode_b'] = {
                                'status': 'failed',
                                'error': type(exc).__name__,
                            }
                finally:
                    reset_tenant_org_id(token)

            result = {
                'status': 'completed',
                'part1': part1,
                'internal_policy_count': internal_policy_count,
                'orgs_processed': len(org_ids),
                'policies_analyzed': policies_analyzed,
                'gap_analysis_runs': gap_runs,
                'redlines_generated': redlines_generated,
                'reference_policies_updated': reference_policies_updated,
                'scraped_sources': sorted(scraped_sources),
                'org_results': org_results,
            }
            logger.info('regulatory_compliance_workflow_completed', org_id=org_id, **{
                key: value for key, value in result.items() if key != 'org_results'
            })
            return result
        except RuntimeError as exc:
            logger.warning(
                'regulatory_compliance_workflow_skipped',
                org_id=org_id,
                reason=str(exc),
            )
            return {'status': 'skipped', 'reason': str(exc)}
        except Exception as exc:
            logger.exception(
                'regulatory_compliance_workflow_failed',
                org_id=org_id,
                error_type=type(exc).__name__,
            )
            return {'status': 'failed', 'error': str(exc)}


async def run_regulatory_compliance_workflow_background(org_id: str | None = None) -> None:
    """Run the compliance workflow outside the HTTP request lifecycle."""
    schedule_regulatory_compliance_workflow(org_id=org_id)
