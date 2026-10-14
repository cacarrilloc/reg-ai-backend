"""Shared per-policy compliance pipeline for cron jobs and internal policy uploads."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.observability import langfuse_workflow_trace, normalize_trace_tags
from app.db.session import reset_tenant_org_id, session_scope, set_tenant_org_id
from app.models.core import GapAnalysis, Policy
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.regulatory import RegulatoryIngestResponse
from app.services.document_chunk_service import (
    RegulatorDocumentSimilarity,
    compare_internal_policy_to_regulator_documents,
)
from app.services.gap_analysis_service import run_gap_analysis
from app.services.policy_types import REGULATORY_REFERENCE_TYPE
from app.services.regulatory_monitoring_service import ingest_regulatory_feeds
from app.services.regulatory_reference_policy_service import (
    ALL_REFERENCE_SOURCES,
)

logger = structlog.get_logger(__name__)


@dataclass
class InternalPolicyWorkflowResult:
    policy_id: str
    embedded_chunks: int = 0
    regulator_similarity: list[RegulatorDocumentSimilarity] = field(default_factory=list)
    gap_analysis: AgentRunResponse | None = None
    gap_analysis_ids: list[str] = field(default_factory=list)
    redlines_generated: int = 0
    scraped_regulator: str | None = None
    reference_policies_updated: int = 0
    errors: list[str] = field(default_factory=list)


async def scrape_regulator_websites(
    db: Session | None,
    sources: Iterable[str],
    *,
    org_id: str | None = None,
) -> RegulatoryIngestResponse:
    """Step (1): scrape scoped regulator websites and persist/embed retrieved content.

    Prefer ``db=None`` so pool connections are not held across the HTTP scrape.
    """
    del db  # Callers should not hold a session across this await.
    return await ingest_regulatory_feeds(sources=list(sources), org_id=org_id)


async def scrape_all_regulator_websites(
    db: Session | None = None,
    *,
    org_id: str | None = None,
) -> RegulatoryIngestResponse:
    """Scrape FINRA, SEC, and CFTC government regulation websites."""
    del db
    sources = list(ALL_REFERENCE_SOURCES)
    return await scrape_regulator_websites(None, sources, org_id=org_id)


async def scrape_and_refresh_all_regulator_references(
    db: Session | None = None,
    *,
    org_id: str,
    settings: Settings,
) -> int:
    """Scrape all government sources and refresh every org Regulations reference doc.

    Scrape failures (SEC 503s, timeouts, etc.) must not block Latest Regulations
    refresh or the rest of the upload/cron gap pipeline.

    Does not keep a SQLAlchemy session open across the multi-minute scrape.
    """
    del db
    try:
        scrape_result = await scrape_regulator_websites(
            None, list(ALL_REFERENCE_SOURCES), org_id=org_id
        )
        if scrape_result.errors:
            logger.warning(
                'regulatory_scrape_completed_with_errors',
                org_id=org_id,
                errors=scrape_result.errors[:10],
                fetched_count=scrape_result.fetched_count,
                created_count=scrape_result.created_count,
            )
    except Exception as exc:
        logger.exception(
            'regulatory_scrape_failed_continuing_with_reference_refresh',
            org_id=org_id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
    return await refresh_org_government_reference_embeddings(
        None,
        org_id=org_id,
        settings=settings,
        sources=ALL_REFERENCE_SOURCES,
    )


async def refresh_org_government_reference_embeddings(
    db: Session | None,
    *,
    org_id: str,
    settings: Settings | None = None,
    sources: Iterable[str] | None = None,
) -> int:
    """Step (2): sync scraped regulator content into org Policy Library Latest Regulations docs."""
    del db
    settings = settings or get_settings()
    from app.services.regulatory_reference_policy_service import (
        refresh_government_reference_policies_for_org_releasing,
    )

    updated = await refresh_government_reference_policies_for_org_releasing(
        org_id,
        settings,
        sources=sources,
    )
    return updated


async def _finish_live_regulator_scrape(
    scrape_task: asyncio.Task[int] | None,
    *,
    org_id: str,
    scraped: set[str],
    result: InternalPolicyWorkflowResult | None = None,
    policy_id: str | None = None,
) -> int:
    """Join the background FINRA/SEC/CFTC scrape without dropping its exception."""
    if scrape_task is None:
        return 0
    try:
        scraped_updated = await scrape_task
    except Exception as exc:
        logger.exception(
            'internal_policy_workflow_regulator_scrape_failed',
            org_id=org_id,
            policy_id=policy_id,
            sources=list(ALL_REFERENCE_SOURCES),
            error_type=type(exc).__name__,
        )
        if result is not None:
            result.errors.append('regulator_scrape_failed')
        return 0
    scraped.update(ALL_REFERENCE_SOURCES)
    if result is not None:
        result.scraped_regulator = ','.join(ALL_REFERENCE_SOURCES)
        result.reference_policies_updated = scraped_updated
        logger.info(
            'internal_policy_workflow_all_regulators_scraped',
            org_id=org_id,
            policy_id=policy_id,
            sources=list(ALL_REFERENCE_SOURCES),
            reference_policies_updated=result.reference_policies_updated,
        )
    else:
        logger.info(
            'internal_policy_workflow_batch_scrape_completed',
            org_id=org_id,
            sources=list(ALL_REFERENCE_SOURCES),
            reference_policies_updated=scraped_updated,
        )
    return scraped_updated


async def embed_internal_policy(
    db: Session | None,
    *,
    policy_id: str,
    org_id: str,
    settings: Settings | None = None,
) -> int:
    """Ensure the internal policy document has OpenAI chunk embeddings."""
    from app.services.policy_service import backfill_policy_embeddings_releasing

    del db
    settings = settings or get_settings()
    return await backfill_policy_embeddings_releasing(policy_id, org_id, settings)


def compare_internal_policy_to_government_reference(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
) -> list[RegulatorDocumentSimilarity]:
    """Baseline cosine between internal policy and FINRA, SEC, and CFTC Regulations."""
    return compare_internal_policy_to_regulator_documents(
        db,
        org_id=org_id,
        internal_policy_id=policy_id,
        internal_policy_name=policy_name,
    )


async def run_gap_analysis_for_policy(
    *,
    org_id: str,
    policy_id: str,
) -> AgentRunResponse:
    """Sentence-level RULE/GAP/FIX analysis and Gap Analysis row upsert (upload + cron).

    Same persist rules for upload and cron:
    - Overwrite the same-name Policy Update row and flag it for Request Review again.
    - Insert a Policy Update row when that Internal policy has none.
    - Leave any Business Change row for the same policy in place.
    """
    return await run_gap_analysis(
        AgentRunRequest(
            trigger_type='policy_update',
            trigger_id=policy_id,
            org_id=org_id,
            policy_ids=[policy_id],
            force_reanalysis=True,
        )
    )


async def generate_redlines_for_material_gaps(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    gap_analysis_ids: list[str],
) -> int:
    """No-op: reviews/redlines must be requested explicitly from the Gap Analysis table.

    Automated workflows only create gap analyses. Enqueueing a PolicyRevision would
    put items into the Review Queue without a user request.
    """
    del db, org_id, policy_id, gap_analysis_ids
    return 0


def latest_gap_analysis_ids_for_policy(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
) -> list[str]:
    rows = (
        db.query(GapAnalysis.id)
        .filter(
            GapAnalysis.org_id == org_id,
            GapAnalysis.policy_id == policy_id,
            GapAnalysis.trigger_type == 'policy_update',
            GapAnalysis.trigger_id == policy_id,
        )
        .order_by(GapAnalysis.created_at.desc())
        .limit(3)
        .all()
    )
    return [row[0] for row in rows]


def _load_active_internal_policy(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
) -> Policy | None:
    return (
        db.query(Policy)
        .filter(
            Policy.id == policy_id,
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .one_or_none()
    )


async def process_internal_policy(
    *,
    org_id: str,
    policy_id: str,
    settings: Settings | None = None,
    scrape_associated_regulator: bool = True,
    scraped_sources: set[str] | None = None,
) -> InternalPolicyWorkflowResult:
    """Run the compliance pipeline for one Internal Policy (upload and cron).

    1. When ``scrape_associated_regulator`` is True: live-scrape FINRA, SEC, and CFTC
       and embed all three ``* Regulations`` docs before analysis
    2–5. Sentence-level LLM RULE / GAP / FIX vs all FINRA/SEC/CFTC Regulations documents
    6. Overwrite the Policy Update gap for this policy name (create one if only a
       Business Change row exists). Request Review builds redlines later (not here)
    """
    settings = settings or get_settings()
    result = InternalPolicyWorkflowResult(policy_id=policy_id)
    scraped = scraped_sources if scraped_sources is not None else set()

    with session_scope(commit=False, org_id=org_id) as scoped_db:
        policy = _load_active_internal_policy(scoped_db, org_id=org_id, policy_id=policy_id)
        if policy is None:
            result.errors.append('policy_not_found')
            return result
        policy_name = policy.name

    async with langfuse_workflow_trace(
        'internal-policy-workflow',
        user_id=org_id,
        session_id=org_id,
        tags=normalize_trace_tags('internal-policy-workflow'),
        metadata={'policy_id': policy_id, 'org_id': org_id},
        input_data={'policy_id': policy_id, 'org_id': org_id, 'policy_name': policy_name},
    ):
        return await _process_internal_policy_steps(
            org_id=org_id,
            policy_id=policy_id,
            policy_name=policy_name,
            result=result,
            settings=settings,
            scrape_associated_regulator=scrape_associated_regulator,
            scraped_sources=scraped,
        )


async def _process_internal_policy_steps(
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
    result: InternalPolicyWorkflowResult,
    settings: Settings,
    scrape_associated_regulator: bool = True,
    scraped_sources: set[str] | None = None,
) -> InternalPolicyWorkflowResult:
    scraped = scraped_sources if scraped_sources is not None else set()
    scrape_task: asyncio.Task[int] | None = None

    if scrape_associated_regulator:
        # Live-scrape FINRA/SEC/CFTC on every upload, but do not block embed/gap
        # on the HTTP scrape. Refresh already-stored pubs so analysis has vectors now.
        scrape_task = asyncio.create_task(
            scrape_and_refresh_all_regulator_references(
                None,
                org_id=org_id,
                settings=settings,
            )
        )
        try:
            result.reference_policies_updated = await refresh_org_government_reference_embeddings(
                None,
                org_id=org_id,
                settings=settings,
                sources=ALL_REFERENCE_SOURCES,
            )
        except Exception as refresh_exc:
            logger.exception(
                'internal_policy_workflow_reference_refresh_failed',
                org_id=org_id,
                policy_id=policy_id,
                error_type=type(refresh_exc).__name__,
            )
            result.errors.append('reference_refresh_failed')

    try:
        try:
            result.embedded_chunks = await embed_internal_policy(
                None,
                policy_id=policy_id,
                org_id=org_id,
                settings=settings,
            )
        except Exception as exc:
            logger.exception(
                'internal_policy_workflow_embedding_failed',
                org_id=org_id,
                policy_id=policy_id,
                error_type=type(exc).__name__,
            )
            result.errors.append('embedding_failed')

        # Keep baseline Internal Policy ↔ Regulations cosine for retrieval metadata.
        # Sentence-level RULE/GAP/FIX findings drive Request Review redlines (no LLM analysis doc).
        with session_scope(commit=False, org_id=org_id) as scoped_db:
            result.regulator_similarity = compare_internal_policy_to_government_reference(
                scoped_db,
                org_id=org_id,
                policy_id=policy_id,
                policy_name=policy_name,
            )

        # Always run sentence-level RULE/GAP/FIX and persist a Policy Update gap.
        try:
            result.gap_analysis = await run_gap_analysis_for_policy(
                org_id=org_id,
                policy_id=policy_id,
            )
        except Exception as exc:
            logger.exception(
                'internal_policy_workflow_gap_analysis_failed',
                org_id=org_id,
                policy_id=policy_id,
                error_type=type(exc).__name__,
            )
            result.errors.append('gap_analysis_failed')
            return result

        with session_scope(commit=True, org_id=org_id) as scoped_db:
            from app.services.gap_analysis_service import attach_regulator_similarity_to_policy_update_gap

            similarities = list(result.regulator_similarity)
            if not similarities:
                similarities = compare_internal_policy_to_government_reference(
                    scoped_db,
                    org_id=org_id,
                    policy_id=policy_id,
                    policy_name=policy_name,
                )
                result.regulator_similarity = similarities
            attach_regulator_similarity_to_policy_update_gap(
                scoped_db,
                org_id=org_id,
                policy_id=policy_id,
                similarities=similarities,
            )
            result.gap_analysis_ids = latest_gap_analysis_ids_for_policy(
                scoped_db,
                org_id=org_id,
                policy_id=policy_id,
            )
        # Never auto-enqueue Review Queue items; user must click Request review.
        result.redlines_generated = 0
    finally:
        await _finish_live_regulator_scrape(
            scrape_task,
            org_id=org_id,
            scraped=scraped,
            result=result,
            policy_id=policy_id,
        )

    logger.info(
        'internal_policy_workflow_completed',
        org_id=org_id,
        policy_id=policy_id,
        embedded_chunks=result.embedded_chunks,
        scraped_regulator=result.scraped_regulator,
        reference_policies_updated=result.reference_policies_updated,
        regulator_similarity_count=len(result.regulator_similarity),
        gap_status=result.gap_analysis.status if result.gap_analysis else None,
        redlines_generated=result.redlines_generated,
    )
    return result


async def process_all_internal_policies_for_org(
    org_id: str,
    *,
    settings: Settings | None = None,
    policy_ids: list[str] | None = None,
    skip_scrape: bool = False,
    scrape_sources: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Loop every target Internal Policy through the shared upload/cron gap pipeline.

    Cron (no ``policy_ids``): live-scrape FINRA/SEC/CFTC, embed those regulation
    docs, then analyze every ACTIVE Internal Policy. Policy Update gaps are
    overwritten; missing Policy Update rows are created beside any Business
    Change row.

    Upload (one ``policy_id``): start a live FINRA/SEC/CFTC scrape in the
    background (``skip_scrape=False``), refresh already-stored regulation
    embeddings, then analyze. The scrape finishes before the workflow returns.

    Redlines stay behind Request Review. Overwriting a Policy Update gap unqueues
    a pending CCO item and flags the row so the user must decide again.
    """
    del scrape_sources  # Retained for call-site compatibility; batch scrape uses all sources.
    settings = settings or get_settings()
    token = set_tenant_org_id(org_id)
    try:
        return await _process_all_internal_policies_for_org_inner(
            org_id,
            settings=settings,
            policy_ids=policy_ids,
            skip_scrape=skip_scrape,
        )
    finally:
        reset_tenant_org_id(token)


async def _process_all_internal_policies_for_org_inner(
    org_id: str,
    *,
    settings: Settings,
    policy_ids: list[str] | None,
    skip_scrape: bool,
) -> dict[str, Any]:
    scraped_sources: set[str] = set()
    batch_reference_policies_updated = 0
    scrape_task: asyncio.Task[int] | None = None

    with session_scope(commit=False, org_id=org_id) as db:
        if policy_ids is not None:
            target_policy_ids = list(policy_ids)
        else:
            target_policy_ids = [
                row[0]
                for row in db.query(Policy.id)
                .filter(
                    Policy.org_id == org_id,
                    Policy.status == 'ACTIVE',
                    Policy.type != REGULATORY_REFERENCE_TYPE,
                )
                .order_by(Policy.name.asc())
                .all()
            ]

    if not skip_scrape:
        scrape_task = asyncio.create_task(
            scrape_and_refresh_all_regulator_references(
                None,
                org_id=org_id,
                settings=settings,
            )
        )
        try:
            batch_reference_policies_updated = await refresh_org_government_reference_embeddings(
                None,
                org_id=org_id,
                settings=settings,
                sources=ALL_REFERENCE_SOURCES,
            )
        except Exception as refresh_exc:
            logger.exception(
                'internal_policy_workflow_batch_reference_refresh_failed',
                org_id=org_id,
                error_type=type(refresh_exc).__name__,
            )

    logger.info(
        'internal_policy_workflow_batch_started',
        org_id=org_id,
        policy_count=len(target_policy_ids),
        reference_policies_updated=batch_reference_policies_updated,
        scrape=not skip_scrape,
    )

    results: list[InternalPolicyWorkflowResult] = []
    try:
        for policy_id in target_policy_ids:
            logger.info(
                'internal_policy_workflow_loop_started',
                org_id=org_id,
                policy_id=policy_id,
                scrape=False,
            )
            policy_result = await process_internal_policy(
                org_id=org_id,
                policy_id=policy_id,
                settings=settings,
                scrape_associated_regulator=False,
                scraped_sources=scraped_sources,
            )
            if batch_reference_policies_updated and not policy_result.reference_policies_updated:
                policy_result.reference_policies_updated = batch_reference_policies_updated
            results.append(policy_result)
    finally:
        scraped_updated = await _finish_live_regulator_scrape(
            scrape_task,
            org_id=org_id,
            scraped=scraped_sources,
        )
        if scraped_updated:
            batch_reference_policies_updated = scraped_updated
            for policy_result in results:
                if not policy_result.reference_policies_updated:
                    policy_result.reference_policies_updated = scraped_updated
                if policy_result.scraped_regulator is None:
                    policy_result.scraped_regulator = ','.join(ALL_REFERENCE_SOURCES)

    return {
        'org_id': org_id,
        'policies_processed': len(results),
        'embedded_chunks': sum(item.embedded_chunks for item in results),
        'redlines_generated': sum(item.redlines_generated for item in results),
        'gap_analysis_runs': sum(1 for item in results if item.gap_analysis is not None),
        'scraped_sources': sorted(scraped_sources),
        'scrape': None,
        'reference_policies_updated': batch_reference_policies_updated
        or sum(item.reference_policies_updated for item in results),
        'errors': [error for item in results for error in item.errors],
    }
