"""Internal IPX organizations bound to the platform Supabase project per env.

Each deploy environment owns exactly one internal organization:
  local/dev  → Development  → existing Dev Reg AI project
  staging    → Staging      → existing Staging Reg AI project
  production → Production   → existing Prod Reg AI project

These orgs never trigger Management-API project creation.

Platform super-admin (``MASTER_ACCOUNT``) always lands on this environment's
internal org so the org name matches ENVIRONMENT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import Organization
from app.services.entitlement_service import seed_default_entitlements
from app.services.organization_tenant_service import bind_organization_to_platform_project

logger = structlog.get_logger(__name__)

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

ORG_KIND_INTERNAL = 'internal'
ORG_KIND_EXTERNAL = 'external'

VALID_ORG_KINDS = frozenset({ORG_KIND_INTERNAL, ORG_KIND_EXTERNAL})


@dataclass(frozen=True)
class InternalEnvironmentOrg:
    """Fixed identity for the internal org belonging to a deploy environment."""

    auth_org_id: str
    slug: str
    name: str
    environment_keys: frozenset[str]
    expected_project_ref: str


INTERNAL_ENVIRONMENT_ORGS: tuple[InternalEnvironmentOrg, ...] = (
    InternalEnvironmentOrg(
        auth_org_id='org_internal_development',
        slug='development',
        name='Development',
        environment_keys=frozenset({'local', 'development', 'dev'}),
        expected_project_ref='ugnfslbacesddjiyjhvp',
    ),
    InternalEnvironmentOrg(
        auth_org_id='org_internal_staging',
        slug='staging',
        name='Staging',
        environment_keys=frozenset({'staging', 'stage', 'test'}),
        expected_project_ref='wtlxahvapeatzmsfdqhs',
    ),
    InternalEnvironmentOrg(
        auth_org_id='org_internal_production',
        slug='production',
        name='Production',
        environment_keys=frozenset({'production', 'prod'}),
        expected_project_ref='rednxostzguusaagguzo',
    ),
)

RESERVED_INTERNAL_SLUGS = frozenset(org.slug for org in INTERNAL_ENVIRONMENT_ORGS)
RESERVED_INTERNAL_NAMES = frozenset(org.name.casefold() for org in INTERNAL_ENVIRONMENT_ORGS)
RESERVED_INTERNAL_AUTH_ORG_IDS = frozenset(
    org.auth_org_id for org in INTERNAL_ENVIRONMENT_ORGS
)

def master_account_email(settings: Settings | None = None) -> str:
    """Normalized ``MASTER_ACCOUNT`` email for this deploy."""
    settings = settings or get_settings()
    return str(settings.master_account or '').strip().lower()


def is_platform_super_admin_email(
    email: str | None,
    *,
    settings: Settings | None = None,
) -> bool:
    """True when ``email`` matches ``MASTER_ACCOUNT`` (platform super-admin)."""
    normalized = str(email or '').strip().lower()
    master = master_account_email(settings)
    return bool(normalized) and bool(master) and normalized == master


def is_uuid_string(value: str | None) -> bool:
    """True when ``value`` is a bare UUID (common accidental org-name fallback)."""
    return bool(_UUID_RE.match(str(value or '').strip()))


def is_bootstrap_uuid_organization(organization: Organization | None) -> bool:
    """Detect external orgs created with UUID as both id and display name.

    ``get_or_create_organization`` falls back to ``name = auth_org_id`` when no
    display name is provided; platform super-admin login must not keep those.
    """
    if organization is None:
        return False
    auth_org_id = str(getattr(organization, 'auth_org_id', '') or '').strip()
    name = str(getattr(organization, 'name', '') or '').strip()
    if not auth_org_id or is_reserved_internal_auth_org_id(auth_org_id):
        return False
    kind = str(getattr(organization, 'kind', '') or '').strip().lower()
    if kind == ORG_KIND_INTERNAL:
        return False
    if not is_uuid_string(auth_org_id):
        return False
    return name == auth_org_id or is_uuid_string(name)


def environment_organization_name(settings: Settings | None = None) -> str | None:
    """Title-case org name for the current ENVIRONMENT (Development/Staging/Production)."""
    definition = resolve_internal_environment_org(settings=settings)
    return definition.name if definition else None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_environment(environment: str | None) -> str:
    return str(environment or 'local').strip().lower() or 'local'


def resolve_internal_environment_org(
    environment: str | None = None,
    *,
    settings: Settings | None = None,
) -> InternalEnvironmentOrg | None:
    """Return the internal org definition for this deploy environment, if any."""
    settings = settings or get_settings()
    env = normalize_environment(environment if environment is not None else settings.environment)
    for org in INTERNAL_ENVIRONMENT_ORGS:
        if env in org.environment_keys:
            return org
    return None


def platform_supabase_project_ref(settings: Settings | None = None) -> str | None:
    """Extract the Supabase project ref from SUPABASE_URL (host subdomain)."""
    settings = settings or get_settings()
    raw = (settings.supabase_url or '').strip()
    if not raw:
        return None
    host = (urlparse(raw).hostname or '').strip().lower()
    if host.endswith('.supabase.co'):
        return host.split('.', 1)[0] or None
    return None


def is_reserved_internal_auth_org_id(auth_org_id: str | None) -> bool:
    """True for the fixed Internal Development / Staging / Production org ids only."""
    return str(auth_org_id or '').strip() in RESERVED_INTERNAL_AUTH_ORG_IDS


def is_reserved_internal_org_name(name: str) -> bool:
    return str(name or '').strip().casefold() in RESERVED_INTERNAL_NAMES


def is_reserved_internal_slug(slug: str | None) -> bool:
    return str(slug or '').strip().lower() in RESERVED_INTERNAL_SLUGS


def is_internal_organization(organization: Organization | None) -> bool:
    if organization is None:
        return False
    kind = str(getattr(organization, 'kind', '') or '').strip().lower()
    if kind == ORG_KIND_INTERNAL:
        return True
    auth_org_id = str(getattr(organization, 'auth_org_id', '') or '').strip()
    slug = str(getattr(organization, 'slug', '') or '').strip().lower()
    return (
        auth_org_id in RESERVED_INTERNAL_AUTH_ORG_IDS
        or slug in RESERVED_INTERNAL_SLUGS
    )


def seed_internal_environment_organization(
    db: Session,
    *,
    settings: Settings | None = None,
) -> Organization | None:
    """Ensure this env's internal org exists and is bound to the platform project.

    Does not create a new Supabase project. Credentials come from the deploy
    env's DATABASE_URL / SUPABASE_* settings and are stored on organization_tenants
    as ``ready`` for routing consistency.
    """
    settings = settings or get_settings()
    definition = resolve_internal_environment_org(settings=settings)
    if definition is None:
        logger.warning(
            'internal_org_seed_skipped_unknown_environment',
            environment=settings.environment,
        )
        return None

    project_ref = platform_supabase_project_ref(settings)
    if project_ref and project_ref != definition.expected_project_ref:
        logger.warning(
            'internal_org_supabase_project_ref_mismatch',
            environment=settings.environment,
            expected=definition.expected_project_ref,
            actual=project_ref,
            hint='SUPABASE_URL should point at the Reg-AI project for this environment.',
        )

    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == definition.auth_org_id)
        .one_or_none()
    )
    created = False
    if organization is None:
        from app.services.organization_service import DEFAULT_REGULATORY_SCOPE

        organization = Organization(
            id=str(uuid4()),
            auth_org_id=definition.auth_org_id,
            name=definition.name,
            slug=definition.slug,
            kind=ORG_KIND_INTERNAL,
            regulatory_scope=dict(DEFAULT_REGULATORY_SCOPE),
            approval_chain={},
            escalation_days=7,
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(organization)
        db.flush([organization])
        seed_default_entitlements(db, auth_org_id=definition.auth_org_id)
        from app.services.role_permissions_service import ensure_organization_role_permissions

        ensure_organization_role_permissions(db, definition.auth_org_id)
        created = True
    else:
        changed = False
        if organization.name != definition.name:
            organization.name = definition.name
            changed = True
        if organization.slug != definition.slug:
            organization.slug = definition.slug
            changed = True
        if str(getattr(organization, 'kind', '') or '') != ORG_KIND_INTERNAL:
            organization.kind = ORG_KIND_INTERNAL
            changed = True
        if changed:
            organization.updated_at = _now()
        # Keep Product 1 + Product 2 enabled for existing internal orgs too.
        seed_default_entitlements(db, auth_org_id=definition.auth_org_id)
        from app.services.role_permissions_service import ensure_organization_role_permissions

        ensure_organization_role_permissions(db, definition.auth_org_id)

    tenant = bind_organization_to_platform_project(
        db,
        auth_org_id=definition.auth_org_id,
        settings=settings,
        expected_project_ref=definition.expected_project_ref,
    )

    member_ids = [
        item.strip()
        for item in str(settings.internal_org_member_user_ids or '').split(',')
        if item.strip()
    ]
    if member_ids:
        from app.core.roles import MASTER_USER
        from app.services.org_lifecycle_service import (
            MEMBER_STATUS_ACTIVE,
            upsert_member,
        )

        for user_id in member_ids:
            upsert_member(
                db,
                auth_org_id=definition.auth_org_id,
                auth_user_id=user_id,
                role=MASTER_USER,
                status=MEMBER_STATUS_ACTIVE,
                org_name=definition.name,
            )

    db.commit()
    db.refresh(organization)

    logger.info(
        'internal_org_seeded',
        created=created,
        auth_org_id=definition.auth_org_id,
        name=definition.name,
        slug=definition.slug,
        supabase_project_ref=tenant.supabase_project_ref if tenant else None,
        tenant_status=tenant.status if tenant else None,
        member_bootstrap_count=len(member_ids),
    )
    return organization
