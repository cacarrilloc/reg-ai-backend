"""Provision / look up per-organization app stack bindings (control plane)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import OrganizationTenant

logger = structlog.get_logger(__name__)

TENANT_STATUS_PENDING = 'pending'
TENANT_STATUS_PROVISIONING = 'provisioning'
TENANT_STATUS_READY = 'ready'
TENANT_STATUS_FAILED = 'failed'

ISOLATION_SHARED = 'shared'
ISOLATION_SCHEMA = 'schema'
ISOLATION_PROJECT = 'project'

_MAX_PROVISION_ERROR = 2000


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _project_ref_from_supabase_url(supabase_url: str | None) -> str | None:
    raw = (supabase_url or '').strip()
    if not raw:
        return None
    host = (urlparse(raw).hostname or '').strip().lower()
    if host.endswith('.supabase.co'):
        return host.split('.', 1)[0] or None
    return None


def _truncate_error(message: str) -> str:
    text = (message or '').strip()
    if len(text) <= _MAX_PROVISION_ERROR:
        return text
    return text[: _MAX_PROVISION_ERROR - 3] + '...'


def get_organization_tenant(
    db: Session,
    *,
    auth_org_id: str,
) -> OrganizationTenant | None:
    auth_org_id = auth_org_id.strip()
    if not auth_org_id:
        return None
    return (
        db.query(OrganizationTenant)
        .filter(OrganizationTenant.auth_org_id == auth_org_id)
        .one_or_none()
    )


def ensure_organization_tenant(
    db: Session,
    *,
    auth_org_id: str,
    settings: Settings | None = None,
) -> OrganizationTenant:
    """Ensure a tenant registry row exists for the Clerk Organization.

    Customer orgs queue an isolated Postgres schema on this Supabase project
    when provisioning is enabled. Schema-mode tenants must be ``ready`` before
    data-plane requests run; they must not fall back to the shared public schema.
    """
    settings = settings or get_settings()
    auth_org_id = auth_org_id.strip()
    existing = get_organization_tenant(db, auth_org_id=auth_org_id)
    if existing is not None:
        return existing

    tenant = OrganizationTenant(
        id=str(uuid4()),
        auth_org_id=auth_org_id,
        status=TENANT_STATUS_PENDING,
        isolation_mode=ISOLATION_SCHEMA,
        schema_name=None,
        supabase_project_ref=None,
        database_url=None,
        supabase_url=None,
        supabase_service_role_key=None,
        backend_url=None,
        frontend_url=str(settings.frontend_url).rstrip('/') or None,
        provision_error='Awaiting isolated schema provisioning for this organization.',
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(tenant)
    logger.info(
        'organization_tenant_queued',
        auth_org_id=auth_org_id,
        status=TENANT_STATUS_PENDING,
        frontend_url=tenant.frontend_url,
    )
    return tenant


def mark_tenant_provisioning(
    db: Session,
    *,
    auth_org_id: str,
    supabase_project_ref: str | None = None,
) -> OrganizationTenant | None:
    tenant = get_organization_tenant(db, auth_org_id=auth_org_id)
    if tenant is None:
        return None
    tenant.status = TENANT_STATUS_PROVISIONING
    if supabase_project_ref:
        tenant.supabase_project_ref = supabase_project_ref
    tenant.provision_error = None
    tenant.updated_at = _now()
    return tenant


def mark_tenant_failed(
    db: Session,
    *,
    auth_org_id: str,
    error: str,
    supabase_project_ref: str | None = None,
) -> OrganizationTenant | None:
    tenant = get_organization_tenant(db, auth_org_id=auth_org_id)
    if tenant is None:
        return None
    tenant.status = TENANT_STATUS_FAILED
    if supabase_project_ref:
        tenant.supabase_project_ref = supabase_project_ref
    tenant.provision_error = _truncate_error(error)
    tenant.updated_at = _now()
    return tenant


def mark_tenant_ready(
    db: Session,
    *,
    auth_org_id: str,
    supabase_project_ref: str,
    database_url: str,
    supabase_url: str | None = None,
    supabase_service_role_key: str | None = None,
    backend_url: str | None = None,
    frontend_url: str | None = None,
    schema_name: str | None = None,
    isolation_mode: str | None = None,
) -> OrganizationTenant | None:
    """Operator/provisioner callback once the org data plane exists."""
    tenant = get_organization_tenant(db, auth_org_id=auth_org_id)
    if tenant is None:
        return None
    tenant.status = TENANT_STATUS_READY
    tenant.supabase_project_ref = supabase_project_ref
    tenant.database_url = database_url
    if schema_name is not None:
        tenant.schema_name = schema_name
    if isolation_mode:
        tenant.isolation_mode = isolation_mode
    if supabase_url:
        tenant.supabase_url = supabase_url
    if supabase_service_role_key:
        tenant.supabase_service_role_key = supabase_service_role_key
    if backend_url:
        tenant.backend_url = backend_url
    if frontend_url:
        tenant.frontend_url = frontend_url
    tenant.provision_error = None
    tenant.updated_at = _now()
    try:
        from app.db.session import clear_tenant_binding_cache

        clear_tenant_binding_cache(auth_org_id)
    except Exception:  # noqa: BLE001 — cache clear must not fail provisioning
        pass
    return tenant


def bind_organization_to_platform_project(
    db: Session,
    *,
    auth_org_id: str,
    settings: Settings | None = None,
    expected_project_ref: str | None = None,
) -> OrganizationTenant | None:
    """Bind an organization to this deploy env's existing Supabase project.

    Used for internal Development / Staging / Production workspaces and for
    organizations created by MASTER_ACCOUNT so they share this environment's
    public schema instead of receiving a dedicated tenant schema.
    """
    settings = settings or get_settings()
    auth_org_id = auth_org_id.strip()
    if not auth_org_id:
        return None

    database_url = (settings.database_url or settings.direct_url or '').strip()
    supabase_url = (settings.supabase_url or '').strip() or None
    service_role = (settings.supabase_service_role_key or '').strip() or None
    project_ref = _project_ref_from_supabase_url(supabase_url) or (
        (expected_project_ref or '').strip() or None
    )
    if not database_url or not project_ref:
        logger.warning(
            'platform_tenant_bind_incomplete',
            auth_org_id=auth_org_id,
            has_database_url=bool(database_url),
            project_ref=project_ref,
        )
        tenant = ensure_organization_tenant(db, auth_org_id=auth_org_id, settings=settings)
        tenant.status = TENANT_STATUS_FAILED
        tenant.provision_error = _truncate_error(
            'Cannot bind internal org: DATABASE_URL and SUPABASE_URL are required.'
        )
        tenant.updated_at = _now()
        return tenant

    ensure_organization_tenant(db, auth_org_id=auth_org_id, settings=settings)
    db.flush()
    return mark_tenant_ready(
        db,
        auth_org_id=auth_org_id,
        supabase_project_ref=project_ref,
        database_url=database_url,
        supabase_url=supabase_url,
        supabase_service_role_key=service_role,
        frontend_url=str(settings.frontend_url).rstrip('/') or None,
        schema_name='public',
        isolation_mode=ISOLATION_SHARED,
    )
