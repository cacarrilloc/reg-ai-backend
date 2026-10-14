from collections.abc import Iterable

import structlog

from app.db.session import SessionLocal, platform_session_scope
from app.services.newsletter_service import send_weekly_newsletter
from app.services.regulatory_compliance_workflow_service import (
    run_regulatory_compliance_workflow,
)
from app.services.regulatory_monitoring_service import ingest_regulatory_feeds
from app.services.supabase_tenant_provisioner import provision_organization_tenant

logger = structlog.get_logger(__name__)


async def run_regulatory_scrape(sources: Iterable[str]) -> dict:
    """Run a scheduled scrape without holding a DB session across HTTP fetches."""
    return (await ingest_regulatory_feeds(sources=sources)).model_dump()


async def run_provision_organization_tenant_job(auth_org_id: str) -> dict:
    """Inngest entrypoint: provision an isolated org schema (or legacy project)."""
    with platform_session_scope() as db:
        return provision_organization_tenant(db, auth_org_id=auth_org_id)


async def run_regulatory_compliance_workflow_job(org_id: str | None = None) -> dict:
    """Inngest cron: scrape, embed, then refresh every org Internal Policy gap."""
    result = await run_regulatory_compliance_workflow(org_id=org_id)
    return {
        **result,
        'org_id': org_id,
    }


async def run_regulatory_sync_background(org_id: str | None = None) -> None:
    """Run regulator scrape + policy review after the Sync now HTTP response is sent.

    Await the workflow directly in FastAPI BackgroundTasks. Do not wrap in
    ``asyncio.create_task`` here — nested tasks can be dropped when the background
    callback returns before the child starts, which made Sync now appear to no-op.
    """
    if SessionLocal is None:
        logger.warning('regulatory_sync_skipped', reason='database_not_configured')
        return

    logger.info('regulatory_sync_started', org_id=org_id)
    try:
        result = await run_regulatory_compliance_workflow(org_id=org_id)
        logger.info(
            'regulatory_sync_completed',
            org_id=org_id,
            status=result.get('status'),
            orgs_processed=result.get('orgs_processed'),
            reference_policies_updated=result.get('reference_policies_updated'),
            scraped_sources=result.get('scraped_sources'),
        )
    except Exception:
        logger.exception('regulatory_sync_failed', org_id=org_id)
        raise


async def run_weekly_newsletter_job() -> dict:
    """Monday cron: one shared brief emailed to every org and public subscriber."""
    return send_weekly_newsletter().model_dump()


async def run_daily_regulatory_scrape() -> dict:
    """Retain the all-sources worker entrypoint for local/manual execution."""
    return await run_regulatory_compliance_workflow(org_id=None)
