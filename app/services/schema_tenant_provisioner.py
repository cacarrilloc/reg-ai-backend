"""Provision one Postgres schema per customer organization on the platform project."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

import psycopg
import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import Organization, OrganizationTenant
from app.services.organization_tenant_service import (
    ISOLATION_SCHEMA,
    TENANT_STATUS_READY,
    ensure_organization_tenant,
    get_organization_tenant,
    mark_tenant_failed,
    mark_tenant_provisioning,
    mark_tenant_ready,
)
from app.services.supabase_tenant_provisioner import _psycopg_dsn

logger = structlog.get_logger(__name__)

TENANT_SCHEMA_RE = re.compile(
    r'^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
)
_TENANT_SUFFIX = '_tenant'
_LEGACY_TENANT_PREFIX = 'tenant_'
_TENANT_SLUG_MAX = 55
_NON_SCHEMA_CHARS = re.compile(r'[^a-z0-9_]')

# Org-owned operational tables. Control-plane and shared feed tables stay in public.
# Covers every sidebar data-plane feature: Policy Library, Business Changes,
# Marketing, Compliance Gaps, CCO Review Queue, Audit History, Support, Insights.
# Regulatory Feed publications stay in public (government-wide); org gaps stay here.
DATA_PLANE_TABLES: tuple[str, ...] = (
    'policies',
    'regulations',
    'context_documents',
    'document_chunks',
    'business_changes',
    'business_change_event_deliveries',
    'gap_analyses',
    'policy_revisions',
    'audit_logs',
    'compliance_history',
    'marketing_contents',
    'marketing_reviews',
    'support_tickets',
    'usage_counters',
    'newsletter_subscribers',
    'ai_provider_credentials',
    'ai_reembed_vectors',
)

# Sidebar / product surfaces that must run entirely against the org schema.
# Regulatory Feed reads public.regulatory_publications (shared government feed).
SIDEBAR_DATA_PLANE_FEATURES: dict[str, tuple[str, ...]] = {
    'Policy Library Reviewer': (
        'policies',
        'document_chunks',
        'context_documents',
        'regulations',
    ),
    'Business Changes Reviewer': (
        'business_changes',
        'business_change_event_deliveries',
    ),
    'Marketing Content Reviewer': ('marketing_contents', 'marketing_reviews'),
    'Compliance Gaps': (
        'gap_analyses',
        'document_chunks',
        'policies',
        'regulations',
    ),
    'CCO Review Queue': ('policy_revisions', 'gap_analyses'),
    'Audit History': ('audit_logs', 'compliance_history'),
    'Support': ('support_tickets',),
    'Insights': (
        'usage_counters',
        'gap_analyses',
        'compliance_history',
        'policies',
        'policy_revisions',
    ),
}


def quote_pg_ident(value: str) -> str:
    return '"' + value.replace('"', '') + '"'


def _sqlalchemy_url_to_libpq(url: str) -> str:
    raw = (url or '').strip()
    if raw.startswith('postgresql+psycopg://'):
        return 'postgresql://' + raw.removeprefix('postgresql+psycopg://')
    if raw.startswith('postgresql+psycopg2://'):
        return 'postgresql://' + raw.removeprefix('postgresql+psycopg2://')
    return raw


def database_usernames_for_grants(settings: Settings) -> tuple[str, ...]:
    """Roles that must be able to use a tenant schema (pooler + direct users)."""
    roles: list[str] = []
    seen: set[str] = set()
    for raw in (settings.database_url, settings.direct_url):
        url = _sqlalchemy_url_to_libpq(raw or '')
        if not url:
            continue
        username = unquote(urlparse(url).username or '').strip()
        if not username or username in seen:
            continue
        seen.add(username)
        roles.append(username)
    return tuple(roles)


def grant_tenant_schema_privileges(
    conn: Any,
    schema_name: str,
    roles: tuple[str, ...] | list[str],
) -> None:
    """Grant USAGE/ALL on the tenant schema after REVOKE FROM PUBLIC."""
    ident = quote_pg_ident(schema_name)
    for role in roles:
        username = (role or '').strip()
        if not username:
            continue
        role_ident = quote_pg_ident(username)
        try:
            conn.execute(f'GRANT USAGE, CREATE ON SCHEMA {ident} TO {role_ident}')
            conn.execute(
                f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA {ident} TO {role_ident}'
            )
            conn.execute(
                f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {ident} TO {role_ident}'
            )
            conn.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA {ident} '
                f'GRANT ALL PRIVILEGES ON TABLES TO {role_ident}'
            )
            conn.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA {ident} '
                f'GRANT ALL PRIVILEGES ON SEQUENCES TO {role_ident}'
            )
        except Exception as exc:  # noqa: BLE001 — missing roles must not abort provision
            logger.warning(
                'tenant_schema_grant_skipped',
                schema_name=schema_name,
                role=username,
                error_type=type(exc).__name__,
            )


def _organization_schema_slug(organization_name: str) -> str:
    """Lowercase org name: dashes become underscores; other invalid characters are dropped."""
    raw = (organization_name or '').strip()
    if not raw:
        raise ValueError('organization name is required')
    lowered = raw.lower()
    if lowered.endswith(_TENANT_SUFFIX) and TENANT_SCHEMA_RE.fullmatch(lowered):
        slug = lowered[: -len(_TENANT_SUFFIX)].strip('_')
        if slug:
            return slug[:_TENANT_SLUG_MAX].rstrip('_')
    if lowered.startswith(_LEGACY_TENANT_PREFIX) and re.fullmatch(
        r'^tenant_[a-z0-9_]{1,55}$', lowered
    ):
        slug = lowered[len(_LEGACY_TENANT_PREFIX) :].strip('_')
        if slug:
            return slug[:_TENANT_SLUG_MAX].rstrip('_')
    slug = lowered.replace('-', '_')
    slug = _NON_SCHEMA_CHARS.sub('', slug)
    slug = re.sub(r'_+', '_', slug).strip('_')
    if slug.endswith(_TENANT_SUFFIX):
        slug = slug[: -len(_TENANT_SUFFIX)].strip('_')
    if slug.startswith(_LEGACY_TENANT_PREFIX):
        slug = slug[len(_LEGACY_TENANT_PREFIX) :].strip('_')
    if not slug:
        raise ValueError('organization name has no valid characters for a schema')
    if slug.startswith('pg'):
        slug = f'org_{slug}'
    return slug[:_TENANT_SLUG_MAX].rstrip('_')


def tenant_schema_name(organization_name: str) -> str:
    """Valid Postgres schema name ``{organization_name}_tenant`` after sanitizing."""
    slug = _organization_schema_slug(organization_name)
    name = f'{slug}{_TENANT_SUFFIX}'
    if not TENANT_SCHEMA_RE.fullmatch(name):
        raise ValueError(
            f'invalid tenant schema name derived from {organization_name!r}'
        )
    return name


def _schema_name_taken(db: Session, schema_name: str, *, auth_org_id: str) -> bool:
    row = (
        db.query(OrganizationTenant)
        .filter(
            OrganizationTenant.schema_name == schema_name,
            OrganizationTenant.auth_org_id != auth_org_id,
        )
        .one_or_none()
    )
    return row is not None


def allocate_tenant_schema_name(
    db: Session,
    *,
    organization_name: str,
    auth_org_id: str,
) -> str:
    """Return ``{org}_tenant``, appending a short org-id before ``_tenant`` if taken."""
    base = tenant_schema_name(organization_name)
    if not _schema_name_taken(db, base, auth_org_id=auth_org_id):
        return base
    suffix = re.sub(r'[^a-z0-9]', '', (auth_org_id or '').lower())[:8] or 'org'
    slug = _organization_schema_slug(organization_name)
    max_slug = _TENANT_SLUG_MAX - len(suffix) - 1
    trimmed = slug[:max_slug].rstrip('_') or 'org'
    candidate = f'{trimmed}_{suffix}{_TENANT_SUFFIX}'
    if not TENANT_SCHEMA_RE.fullmatch(candidate):
        raise ValueError(
            f'invalid tenant schema name derived from {organization_name!r}'
        )
    return candidate


def rewrite_cloned_fk_definition(
    definition: str,
    *,
    referenced_table: str,
    tenant_schema: str,
) -> str:
    """Point a cloned FK at the tenant table, or keep public for shared tables.

    ``pg_get_constraintdef`` omits the ``public.`` qualifier, so a naive
    ``replace('public.', tenant)`` leaves ``REFERENCES policies(id)`` which
    resolves back to ``public.policies`` at ``ALTER TABLE`` time.
    """
    target_schema = (
        tenant_schema if referenced_table in DATA_PLANE_TABLES else 'public'
    )
    quoted = f'{quote_pg_ident(target_schema)}.{quote_pg_ident(referenced_table)}'
    pattern = re.compile(
        r'REFERENCES\s+(?:(?:"?public"?|"?'
        + re.escape(tenant_schema)
        + r'"?)\.)?(?:"?'
        + re.escape(referenced_table)
        + r'"?)\s*\(',
        re.IGNORECASE,
    )
    rewritten, count = pattern.subn(f'REFERENCES {quoted}(', definition, count=1)
    if count != 1:
        raise ValueError(
            f'could not rewrite FK for {referenced_table!r} in {definition!r}'
        )
    return rewritten


def _provisioning_dsn(settings: Settings) -> str:
    url = (settings.direct_url or settings.database_url or '').strip()
    if not url:
        raise RuntimeError('DATABASE_URL or DIRECT_URL is required to provision tenant schemas')
    return _psycopg_dsn(url)


def _project_ref_from_settings(settings: Settings) -> str:
    host = urlparse((settings.supabase_url or '').strip()).hostname or ''
    if host.endswith('.supabase.co'):
        return host.split('.', 1)[0]
    return 'platform'


def create_organization_schema(schema_name: str, *, settings: Settings | None = None) -> None:
    """Create ``schema_name`` and clone data-plane table structures from public."""
    settings = settings or get_settings()
    schema_name = tenant_schema_name(schema_name) if not TENANT_SCHEMA_RE.fullmatch(schema_name) else schema_name
    ident = quote_pg_ident(schema_name)
    dsn = _provisioning_dsn(settings)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS {ident}')
        conn.execute(f'REVOKE ALL ON SCHEMA {ident} FROM PUBLIC')
        existing = {
            row[0]
            for row in conn.execute(
                """
                select table_name
                from information_schema.tables
                where table_schema = %s and table_type = 'BASE TABLE'
                """,
                (schema_name,),
            )
        }
        public_tables = {
            row[0]
            for row in conn.execute(
                """
                select table_name
                from information_schema.tables
                where table_schema = 'public' and table_type = 'BASE TABLE'
                """
            )
        }
        for table in DATA_PLANE_TABLES:
            if table not in public_tables or table in existing:
                continue
            conn.execute(
                f'CREATE TABLE {ident}.{quote_pg_ident(table)} '
                f'(LIKE public.{quote_pg_ident(table)} INCLUDING ALL)'
            )

        conn.execute(
            f"""
            do $body$
            declare
              r record;
            begin
              for r in
                select n.nspname, c.relname, con.conname
                from pg_constraint con
                join pg_class c on c.oid = con.conrelid
                join pg_namespace n on n.oid = c.relnamespace
                where con.contype = 'f' and n.nspname = {schema_name!r}
              loop
                execute format(
                  'alter table %I.%I drop constraint %I',
                  r.nspname, r.relname, r.conname
                );
              end loop;
            end
            $body$;
            """
        )

        fks = conn.execute(
            """
            select src.relname, tgt.relname, pg_get_constraintdef(con.oid)
            from pg_constraint con
            join pg_class src on src.oid = con.conrelid
            join pg_namespace sn on sn.oid = src.relnamespace
            join pg_class tgt on tgt.oid = con.confrelid
            join pg_namespace tn on tn.oid = tgt.relnamespace
            where con.contype = 'f'
              and sn.nspname = 'public'
              and tn.nspname = 'public'
              and src.relname = any(%s)
            """,
            (list(DATA_PLANE_TABLES),),
        ).fetchall()
        for src_table, tgt_table, definition in fks:
            rewritten = rewrite_cloned_fk_definition(
                definition or '',
                referenced_table=tgt_table,
                tenant_schema=schema_name,
            )
            conn.execute(
                f'ALTER TABLE {ident}.{quote_pg_ident(src_table)} ADD {rewritten}'
            )
        grant_tenant_schema_privileges(
            conn, schema_name, database_usernames_for_grants(settings)
        )


def copy_organization_data_into_schema(
    schema_name: str,
    auth_org_id: str,
    *,
    settings: Settings | None = None,
    purge_public: bool = True,
) -> dict[str, int]:
    """Move one org's data-plane rows from public into its dedicated schema.

    Shared regulatory publications and global publication chunks stay in public.
    """
    settings = settings or get_settings()
    schema_name = (
        tenant_schema_name(schema_name)
        if not TENANT_SCHEMA_RE.fullmatch(schema_name)
        else schema_name
    )
    auth_org_id = (auth_org_id or '').strip()
    if not auth_org_id:
        raise ValueError('auth_org_id is required')
    ident = quote_pg_ident(schema_name)
    dsn = _provisioning_dsn(settings)
    copied: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        public_tables = {
            row[0]
            for row in conn.execute(
                """
                select table_name
                from information_schema.tables
                where table_schema = 'public' and table_type = 'BASE TABLE'
                """
            )
        }
        tenant_tables = {
            row[0]
            for row in conn.execute(
                """
                select table_name
                from information_schema.tables
                where table_schema = %s and table_type = 'BASE TABLE'
                """,
                (schema_name,),
            )
        }
        org_id_tables = {
            row[0]
            for row in conn.execute(
                """
                select table_name
                from information_schema.columns
                where table_schema = 'public'
                  and column_name = 'org_id'
                  and table_name = any(%s)
                """,
                (list(DATA_PLANE_TABLES),),
            )
        }
        for table in DATA_PLANE_TABLES:
            if table not in public_tables or table not in tenant_tables:
                continue
            if table not in org_id_tables:
                continue
            quoted = quote_pg_ident(table)
            result = conn.execute(
                f'INSERT INTO {ident}.{quoted} '
                f'SELECT * FROM public.{quoted} WHERE org_id = %s '
                'ON CONFLICT DO NOTHING',
                (auth_org_id,),
            )
            copied[table] = int(result.rowcount or 0)
        if purge_public:
            for table in reversed(DATA_PLANE_TABLES):
                if table not in public_tables or table not in org_id_tables:
                    continue
                conn.execute(
                    f'DELETE FROM public.{quote_pg_ident(table)} WHERE org_id = %s',
                    (auth_org_id,),
                )
        conn.commit()
    logger.info(
        'organization_tenant_schema_rows_copied',
        auth_org_id=auth_org_id,
        schema_name=schema_name,
        copied=copied,
        purged_public=purge_public,
    )
    return copied


def drop_organization_schema(schema_name: str, *, settings: Settings | None = None) -> None:
    """Drop a dedicated tenant schema. Never drops public."""
    settings = settings or get_settings()
    schema_name = (schema_name or '').strip()
    if not TENANT_SCHEMA_RE.fullmatch(schema_name):
        raise ValueError(f'refusing to drop non-tenant schema {schema_name!r}')
    dsn = _provisioning_dsn(settings)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS {quote_pg_ident(schema_name)} CASCADE')
    logger.info('organization_tenant_schema_dropped', schema_name=schema_name)


def _seed_isolated_org_catalog(auth_org_id: str) -> None:
    """Seed marketing-rule catalog rows into the new org schema.

    FINRA/SEC/CFTC Latest Regulations docs are live-scraped and embedded on the
    first Internal Policy upload (or Sync now).
    The four SEC/FINRA marketing rules must exist immediately so Marketing Gaps
    and policy-library cosine comparison can run on an empty tenant schema.
    """
    try:
        from app.db.session import session_scope
        from app.services.regulation_catalog import ensure_marketing_regulations

        with session_scope(org_id=auth_org_id) as tenant_db:
            ensure_marketing_regulations(tenant_db, auth_org_id)
    except Exception as exc:  # noqa: BLE001 — catalog seed must not fail isolation
        logger.warning(
            'tenant_catalog_seed_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )


def provision_organization_schema_tenant(
    db: Session,
    *,
    auth_org_id: str,
    org_name: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Create and bind a dedicated schema for ``auth_org_id`` on the platform DB."""
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
            'schema_name': bound.schema_name if bound else None,
            'skipped': True,
            'reason': 'internal_org_uses_platform_project',
        }

    if tenant.status == TENANT_STATUS_READY and (tenant.schema_name or tenant.database_url):
        return {
            'status': 'ready',
            'auth_org_id': auth_org_id,
            'schema_name': tenant.schema_name,
            'skipped': True,
        }

    if not settings.tenant_provisioning_configured:
        mark_tenant_failed(
            db,
            auth_org_id=auth_org_id,
            error='Tenant schema provisioning disabled or DATABASE_URL is missing.',
        )
        db.commit()
        return {
            'status': 'failed',
            'auth_org_id': auth_org_id,
            'reason': 'provisioning_not_configured',
        }

    display_name = (org_name or '').strip()
    if not display_name and organization is not None:
        display_name = str(getattr(organization, 'name', '') or '').strip()
    schema_name = allocate_tenant_schema_name(
        db,
        organization_name=display_name,
        auth_org_id=auth_org_id,
    )
    mark_tenant_provisioning(db, auth_org_id=auth_org_id)
    row = get_organization_tenant(db, auth_org_id=auth_org_id)
    if row is not None:
        row.schema_name = schema_name
        row.isolation_mode = ISOLATION_SCHEMA
    db.commit()

    try:
        create_organization_schema(schema_name, settings=settings)
        copy_organization_data_into_schema(
            schema_name, auth_org_id, settings=settings
        )
        database_url = (settings.database_url or settings.direct_url or '').strip()
        mark_tenant_ready(
            db,
            auth_org_id=auth_org_id,
            supabase_project_ref=_project_ref_from_settings(settings),
            database_url=database_url,
            supabase_url=(settings.supabase_url or '').strip() or None,
            supabase_service_role_key=(settings.supabase_service_role_key or '').strip() or None,
            frontend_url=str(settings.frontend_url).rstrip('/') or None,
            schema_name=schema_name,
            isolation_mode=ISOLATION_SCHEMA,
        )
        db.commit()
        logger.info(
            'organization_tenant_schema_provisioned',
            auth_org_id=auth_org_id,
            schema_name=schema_name,
        )
        _seed_isolated_org_catalog(auth_org_id)
        return {
            'status': 'ready',
            'auth_org_id': auth_org_id,
            'schema_name': schema_name,
            'skipped': False,
        }
    except Exception as exc:
        logger.exception(
            'organization_tenant_schema_provision_failed',
            auth_org_id=auth_org_id,
            schema_name=schema_name,
            error_type=type(exc).__name__,
        )
        db.rollback()
        mark_tenant_failed(
            db,
            auth_org_id=auth_org_id,
            error=f'{type(exc).__name__}: {exc}',
        )
        db.commit()
        return {
            'status': 'failed',
            'auth_org_id': auth_org_id,
            'schema_name': schema_name,
            'error': str(exc)[:500],
        }
