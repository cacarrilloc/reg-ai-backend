"""Create and bootstrap one Supabase project per Clerk organization."""

from __future__ import annotations

import secrets
import string
import time
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import httpx
import psycopg
import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import Organization
from app.services.organization_tenant_service import (
    TENANT_STATUS_READY,
    ensure_organization_tenant,
    get_organization_tenant,
    mark_tenant_failed,
    mark_tenant_provisioning,
    mark_tenant_ready,
)
from app.services.storage_service import ensure_configured_storage_buckets
from app.services.tenant_migrations import tenant_migration_paths

logger = structlog.get_logger(__name__)

_MANAGEMENT_API = 'https://api.supabase.com/v1'
_HEALTH_TIMEOUT_SECONDS = 900
_HEALTH_POLL_SECONDS = 10
_HTTP_TIMEOUT = 60.0


def _generate_db_password(length: int = 32) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _project_name(auth_org_id: str) -> str:
    suffix = auth_org_id.replace('org_', '')[-12:] or auth_org_id[-12:]
    return f'reg-ai-{suffix}'[:40]


def _build_database_url(*, project_ref: str, db_password: str) -> str:
    user = quote_plus('postgres')
    password = quote_plus(db_password)
    host = f'db.{project_ref}.supabase.co'
    return f'postgresql+psycopg://{user}:{password}@{host}:5432/postgres'


def _psycopg_dsn(database_url: str) -> str:
    if database_url.startswith('postgresql+psycopg://'):
        return 'postgresql://' + database_url.removeprefix('postgresql+psycopg://')
    if database_url.startswith('postgresql+psycopg2://'):
        return 'postgresql://' + database_url.removeprefix('postgresql+psycopg2://')
    return database_url


def _auth_headers(settings: Settings) -> dict[str, str]:
    token = (settings.supabase_access_token or '').strip()
    if not token:
        raise RuntimeError('SUPABASE_ACCESS_TOKEN is not configured')
    return {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }


def _create_project(
    *,
    settings: Settings,
    auth_org_id: str,
    db_password: str,
) -> dict[str, Any]:
    org_slug = (settings.supabase_platform_org_slug or '').strip()
    if not org_slug:
        raise RuntimeError('SUPABASE_PLATFORM_ORG_SLUG is not configured')

    payload: dict[str, Any] = {
        'name': _project_name(auth_org_id),
        'organization_slug': org_slug,
        'db_pass': db_password,
        'desired_instance_size': settings.supabase_tenant_db_instance_size,
    }
    region = (settings.supabase_tenant_region or '').strip()
    if region in {'americas', 'europe', 'apac', 'oceania'}:
        payload['region_selection'] = {'type': 'smartGroup', 'code': region}
    elif region:
        payload['region_selection'] = {'type': 'specific', 'code': region}

    response = httpx.post(
        f'{_MANAGEMENT_API}/projects',
        headers=_auth_headers(settings),
        json=payload,
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f'Supabase create project failed ({response.status_code}): '
            f'{response.text[:500]}'
        )
    data = response.json()
    if not isinstance(data, dict) or not data.get('id') and not data.get('ref'):
        raise RuntimeError(f'Unexpected create project response: {data!r}'[:500])
    return data


def _project_ref(payload: dict[str, Any]) -> str:
    ref = str(payload.get('ref') or payload.get('id') or '').strip()
    if not ref:
        raise RuntimeError('Supabase project response missing ref')
    return ref


def delete_supabase_project(
    *,
    project_ref: str,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Delete a dedicated tenant Supabase project via the Management API.

    Returns a small status dict. ``404`` is treated as already-deleted success.
    """
    settings = settings or get_settings()
    project_ref = (project_ref or '').strip()
    if not project_ref:
        raise ValueError('project_ref is required')

    response = httpx.delete(
        f'{_MANAGEMENT_API}/projects/{project_ref}',
        headers=_auth_headers(settings),
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code in {200, 201, 202, 204}:
        logger.info('supabase_project_deleted', project_ref=project_ref)
        return {'deleted': True, 'project_ref': project_ref, 'status_code': response.status_code}
    if response.status_code == 404:
        logger.info('supabase_project_already_absent', project_ref=project_ref)
        return {
            'deleted': True,
            'project_ref': project_ref,
            'status_code': 404,
            'already_absent': True,
        }
    raise RuntimeError(
        f'Supabase delete project failed ({response.status_code}): {response.text[:500]}'
    )


def _wait_until_healthy(*, settings: Settings, project_ref: str) -> None:
    deadline = time.monotonic() + _HEALTH_TIMEOUT_SECONDS
    last_error = 'not started'
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f'{_MANAGEMENT_API}/projects/{project_ref}/health',
                headers=_auth_headers(settings),
                params={'services': ['db']},
                timeout=_HTTP_TIMEOUT,
            )
            if response.status_code == 200:
                body = response.json()
                services = body if isinstance(body, list) else []
                db_ok = False
                for item in services:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get('name') or '').lower()
                    if name in {'db', 'database', 'postgres', 'postgresql'}:
                        if item.get('healthy') is True or str(item.get('status') or '').upper() in {
                            'ACTIVE_HEALTHY',
                            'HEALTHY',
                        }:
                            db_ok = True
                            break
                if not services:
                    # Some responses omit services once the project is up; fall through
                    # to project status check.
                    project = httpx.get(
                        f'{_MANAGEMENT_API}/projects/{project_ref}',
                        headers=_auth_headers(settings),
                        timeout=_HTTP_TIMEOUT,
                    )
                    if project.status_code == 200:
                        status = str(project.json().get('status') or '').upper()
                        if status in {'ACTIVE_HEALTHY', 'ACTIVE'}:
                            return
                if db_ok:
                    return
                last_error = f'health={body!r}'[:300]
            else:
                last_error = f'status={response.status_code} body={response.text[:200]}'
        except Exception as exc:  # noqa: BLE001 — poll loop must continue
            last_error = f'{type(exc).__name__}: {exc}'[:300]
        time.sleep(_HEALTH_POLL_SECONDS)
    raise RuntimeError(
        f'Timed out waiting for Supabase project {project_ref} healthy: {last_error}'
    )


def _fetch_service_role_key(*, settings: Settings, project_ref: str) -> str:
    response = httpx.get(
        f'{_MANAGEMENT_API}/projects/{project_ref}/api-keys',
        headers=_auth_headers(settings),
        params={'reveal': 'true'},
        timeout=_HTTP_TIMEOUT,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f'Failed to fetch API keys ({response.status_code}): {response.text[:500]}'
        )
    keys = response.json()
    if not isinstance(keys, list):
        raise RuntimeError('Unexpected api-keys response')

    for item in keys:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or item.get('type') or '').lower()
        api_key = str(item.get('api_key') or item.get('key') or '').strip()
        if api_key and ('service_role' in name or name == 'service_role'):
            return api_key

    # Legacy list shape: {"name": "service_role", "api_key": "..."}
    for item in keys:
        if not isinstance(item, dict):
            continue
        if str(item.get('name') or '') == 'service_role':
            api_key = str(item.get('api_key') or '').strip()
            if api_key:
                return api_key

    raise RuntimeError('service_role API key not found for tenant project')


def apply_tenant_migrations(database_url: str) -> None:
    """Execute ordered tenant SQL files against a fresh database."""
    dsn = _psycopg_dsn(database_url)
    with psycopg.connect(dsn, autocommit=True) as conn:
        for path in tenant_migration_paths():
            sql = path.read_text(encoding='utf-8')
            logger.info('tenant_migration_applying', file=path.name)
            conn.execute(sql)


def seed_tenant_organization(
    database_url: str,
    *,
    auth_org_id: str,
    name: str | None = None,
) -> None:
    display = (name or '').strip() or auth_org_id
    dsn = _psycopg_dsn(database_url)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """
            insert into public.organizations (
              id, auth_org_id, name, regulatory_scope, approval_chain,
              escalation_days, created_at, updated_at
            ) values (
              %(id)s, %(auth_org_id)s, %(name)s, '{}'::jsonb, '{}'::jsonb,
              7, now(), now()
            )
            on conflict (auth_org_id) do update
            set name = excluded.name, updated_at = now()
            """,
            {
                'id': str(uuid4()),
                'auth_org_id': auth_org_id,
                'name': display,
            },
        )


def provision_organization_tenant(
    db: Session,
    *,
    auth_org_id: str,
    org_name: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Provision a dedicated Supabase project for ``auth_org_id`` (platform DB session)."""
    settings = settings or get_settings()
    auth_org_id = auth_org_id.strip()
    if not auth_org_id:
        raise ValueError('auth_org_id is required')

    tenant = ensure_organization_tenant(db, auth_org_id=auth_org_id, settings=settings)
    db.flush()

    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == auth_org_id)
        .one_or_none()
    )
    from app.services.internal_org_service import is_internal_organization

    if is_internal_organization(organization):
        from app.services.organization_tenant_service import bind_organization_to_platform_project

        bound = bind_organization_to_platform_project(db, auth_org_id=auth_org_id, settings=settings)
        db.commit()
        return {
            'status': bound.status if bound else 'failed',
            'auth_org_id': auth_org_id,
            'supabase_project_ref': bound.supabase_project_ref if bound else None,
            'skipped': True,
            'reason': 'internal_org_uses_platform_project',
        }

    if tenant.status == TENANT_STATUS_READY and tenant.database_url:
        return {
            'status': 'ready',
            'auth_org_id': auth_org_id,
            'supabase_project_ref': tenant.supabase_project_ref,
            'skipped': True,
        }

    isolation_mode = str(getattr(settings, 'tenant_isolation_mode', 'schema') or 'schema')
    isolation_mode = isolation_mode.strip().lower()
    if isolation_mode != 'project':
        from app.services.schema_tenant_provisioner import provision_organization_schema_tenant

        return provision_organization_schema_tenant(
            db, auth_org_id=auth_org_id, org_name=org_name, settings=settings
        )

    if not settings.tenant_provisioning_configured:
        mark_tenant_failed(
            db,
            auth_org_id=auth_org_id,
            error=(
                'Tenant provisioning disabled or incomplete config '
                '(TENANT_PROVISIONING_ENABLED, SUPABASE_ACCESS_TOKEN, '
                'SUPABASE_PLATFORM_ORG_SLUG).'
            ),
        )
        db.commit()
        return {
            'status': 'failed',
            'auth_org_id': auth_org_id,
            'reason': 'provisioning_not_configured',
        }

    display_name = org_name or (organization.name if organization else None) or auth_org_id

    project_ref = (tenant.supabase_project_ref or '').strip() or None
    db_password = _generate_db_password()
    database_url: str | None = (tenant.database_url or '').strip() or None
    mark_tenant_provisioning(db, auth_org_id=auth_org_id, supabase_project_ref=project_ref)
    db.commit()

    try:
        if not project_ref:
            created = _create_project(
                settings=settings,
                auth_org_id=auth_org_id,
                db_password=db_password,
            )
            project_ref = _project_ref(created)
            echoed = ''
            database = created.get('database')
            if isinstance(database, dict):
                echoed = str(database.get('password') or '')
            echoed = echoed or str(created.get('db_pass') or '')
            if echoed:
                db_password = echoed
            database_url = _build_database_url(
                project_ref=project_ref, db_password=db_password
            )
            mark_tenant_provisioning(
                db, auth_org_id=auth_org_id, supabase_project_ref=project_ref
            )
            # Persist URL early so a crashed run can resume without the password.
            row = get_organization_tenant(db, auth_org_id=auth_org_id)
            if row is not None:
                row.database_url = database_url
            db.commit()
        elif not database_url:
            raise RuntimeError(
                'Cannot resume provisioning without database_url; '
                'clear supabase_project_ref or complete manually.'
            )

        assert project_ref is not None
        assert database_url is not None

        _wait_until_healthy(settings=settings, project_ref=project_ref)
        apply_tenant_migrations(database_url)
        seed_tenant_organization(
            database_url, auth_org_id=auth_org_id, name=display_name
        )

        supabase_url = f'https://{project_ref}.supabase.co'
        service_role_key = _fetch_service_role_key(
            settings=settings, project_ref=project_ref
        )
        ensure_configured_storage_buckets(
            settings,
            supabase_url=supabase_url,
            supabase_service_role_key=service_role_key,
        )

        mark_tenant_ready(
            db,
            auth_org_id=auth_org_id,
            supabase_project_ref=project_ref,
            database_url=database_url,
            supabase_url=supabase_url,
            supabase_service_role_key=service_role_key,
            frontend_url=str(settings.frontend_url).rstrip('/') or None,
            isolation_mode='project',
        )
        db.commit()
        logger.info(
            'organization_tenant_provisioned',
            auth_org_id=auth_org_id,
            supabase_project_ref=project_ref,
        )
        return {
            'status': 'ready',
            'auth_org_id': auth_org_id,
            'supabase_project_ref': project_ref,
            'skipped': False,
        }
    except Exception as exc:
        logger.exception(
            'organization_tenant_provision_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )
        db.rollback()
        mark_tenant_failed(
            db,
            auth_org_id=auth_org_id,
            error=f'{type(exc).__name__}: {exc}',
            supabase_project_ref=project_ref,
        )
        db.commit()
        return {
            'status': 'failed',
            'auth_org_id': auth_org_id,
            'error': str(exc)[:500],
            'supabase_project_ref': project_ref,
        }
