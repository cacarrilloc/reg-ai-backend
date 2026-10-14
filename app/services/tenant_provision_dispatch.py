"""Queue dedicated Supabase project provisioning for a Clerk organization."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from app.core.config import get_settings
from app.db.session import platform_session_scope
from app.services.organization_tenant_service import (
    TENANT_STATUS_FAILED,
    TENANT_STATUS_PENDING,
    TENANT_STATUS_READY,
    ensure_organization_tenant,
)

logger = structlog.get_logger(__name__)
_background_tasks: set[asyncio.Task[Any]] = set()


async def emit_organization_tenant_provision_event(auth_org_id: str) -> bool:
    settings = get_settings()
    if not settings.inngest_async_dispatch_enabled:
        return False
    try:
        import inngest

        from app.core.inngest import create_inngest_client
    except ImportError:
        return False

    client = create_inngest_client(settings)
    try:
        await client.send(
            inngest.Event(
                name='organization.tenant.provision',
                data={'auth_org_id': auth_org_id},
            )
        )
        return True
    except Exception as exc:
        logger.warning(
            'organization_tenant_provision_event_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )
        return False


def _log_background_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.exception(
            'organization_tenant_provision_background_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )


def _provision_tenant_now(auth_org_id: str) -> None:
    from app.services.supabase_tenant_provisioner import provision_organization_tenant

    with platform_session_scope() as db:
        provision_organization_tenant(db, auth_org_id=auth_org_id)


async def _run_provision_background(auth_org_id: str) -> None:
    """Create the org schema off the API event loop so health checks stay up."""
    await asyncio.to_thread(_provision_tenant_now, auth_org_id)


def schedule_organization_tenant_provision(auth_org_id: str) -> None:
    """Ensure tenant row exists and queue provisioning (Inngest or local asyncio)."""
    from app.db.session import SessionLocal

    auth_org_id = (auth_org_id or '').strip()
    if not auth_org_id:
        return
    if SessionLocal is None:
        logger.info(
            'organization_tenant_provision_skipped',
            auth_org_id=auth_org_id,
            reason='database_not_configured',
        )
        return

    settings = get_settings()
    with platform_session_scope() as db:
        tenant = ensure_organization_tenant(db, auth_org_id=auth_org_id, settings=settings)
        status = tenant.status
        db.commit()

    if status == TENANT_STATUS_READY:
        return
    if status == 'provisioning':
        return
    if status not in {TENANT_STATUS_PENDING, TENANT_STATUS_FAILED}:
        return

    async def _dispatch() -> None:
        sent = await emit_organization_tenant_provision_event(auth_org_id)
        if sent:
            logger.info(
                'organization_tenant_provision_queued_inngest',
                auth_org_id=auth_org_id,
            )
            return
        if not settings.tenant_provisioning_configured:
            logger.info(
                'organization_tenant_provision_skipped',
                auth_org_id=auth_org_id,
                reason='provisioning_not_configured',
            )
            return
        logger.info(
            'organization_tenant_provision_queued_local',
            auth_org_id=auth_org_id,
        )
        await _run_provision_background(auth_org_id)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Sync webhook context without a loop — run provision inline when configured.
        if settings.tenant_provisioning_configured and status in {
            TENANT_STATUS_PENDING,
            TENANT_STATUS_FAILED,
        }:
            _provision_tenant_now(auth_org_id)
        return

    task = loop.create_task(_dispatch(), name=f'tenant-provision-{auth_org_id}')
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_background_task_result)
