"""Organization + membership lifecycle for the Supabase Auth control plane.

Organizations are no longer mirrored from a third-party IdP (Clerk). This
module owns creation, membership upserts, and role changes against the local
``organizations`` / ``organization_members`` tables, which are now the
source of truth.
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock
from uuid import uuid4

import structlog
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.roles import DEFAULT_CUSTOMER_CREATOR_ROLE, DEFAULT_INVITE_ROLE
from app.models.core import Organization, OrganizationMember
from app.services.entitlement_service import seed_default_entitlements

logger = structlog.get_logger(__name__)

MEMBER_STATUS_ACTIVE = 'active'
MEMBER_STATUS_INVITED = 'invited'
MEMBER_STATUS_REMOVED = 'removed'

# Org ids that need tenant provisioning after the surrounding transaction commits.
_pending_tenant_provisions: set[str] = set()
_provision_lock = Lock()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _string(value: object) -> str:
    return str(value).strip() if value is not None else ''


def _queue_tenant_provision(auth_org_id: str) -> None:
    with _provision_lock:
        _pending_tenant_provisions.add(auth_org_id)


def flush_pending_tenant_provisions() -> None:
    """Schedule provisioning for orgs created in the last committed transaction."""
    from app.services.tenant_provision_dispatch import schedule_organization_tenant_provision

    with _provision_lock:
        pending = list(_pending_tenant_provisions)
        _pending_tenant_provisions.clear()
    for auth_org_id in pending:
        schedule_organization_tenant_provision(auth_org_id)


def get_or_create_organization(
    db: Session,
    *,
    auth_org_id: str,
    name: str | None = None,
    kind: str | None = None,
    slug: str | None = None,
    isolate_database: bool = True,
) -> Organization:
    """Return the org profile, creating it (and queueing tenant provisioning) if missing.

    External customer orgs queue an isolated Postgres schema when
    ``TENANT_PROVISIONING_ENABLED`` is on (every environment). Internal orgs
    and MASTER_ACCOUNT-created orgs bind to this environment's shared public
    schema. Customer orgs never fall back to the shared data plane.
    """
    from app.core.config import get_settings
    from app.services.internal_org_service import (
        ORG_KIND_EXTERNAL,
        ORG_KIND_INTERNAL,
        RESERVED_INTERNAL_AUTH_ORG_IDS,
        VALID_ORG_KINDS,
        resolve_internal_environment_org,
    )
    from app.services.organization_service import DEFAULT_REGULATORY_SCOPE
    from app.services.organization_tenant_service import (
        bind_organization_to_platform_project,
        ensure_organization_tenant,
    )

    auth_org_id = _string(auth_org_id)
    if not auth_org_id:
        raise ValueError('auth_org_id is required')

    resolved_kind = _string(kind).lower() or ORG_KIND_EXTERNAL
    if resolved_kind not in VALID_ORG_KINDS:
        raise ValueError(f'invalid organization kind: {resolved_kind}')

    display_name = _string(name) or auth_org_id
    resolved_slug = _string(slug) or None
    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if organization is None:
        organization = Organization(
            id=str(uuid4()),
            auth_org_id=auth_org_id,
            name=display_name,
            slug=resolved_slug,
            kind=resolved_kind,
            regulatory_scope=dict(DEFAULT_REGULATORY_SCOPE),
            approval_chain={},
            escalation_days=7,
            created_at=_now(),
            updated_at=_now(),
        )
        # Sessions run with autoflush=False, so the INSERT has to be flushed here:
        # callers upsert the same org several times per request, and an unflushed
        # row stays invisible to the lookup above. The savepoint keeps a race with
        # a concurrent request from poisoning the surrounding transaction.
        try:
            with db.begin_nested():
                db.add(organization)
                db.flush([organization])
        except IntegrityError:
            db.expunge(organization)
            logger.info('organization_create_race_resolved', auth_org_id=auth_org_id)
            return (
                db.query(Organization)
                .filter(Organization.auth_org_id == auth_org_id)
                .one()
            )
        ensure_organization_tenant(db, auth_org_id=auth_org_id)
        seed_default_entitlements(db, auth_org_id=auth_org_id)
        from app.services.role_permissions_service import ensure_organization_role_permissions

        ensure_organization_role_permissions(db, auth_org_id)
        isolate_on_this_env = (
            bool(isolate_database) and get_settings().tenant_provisioning_configured
        )
        if (
            resolved_kind == ORG_KIND_INTERNAL
            or auth_org_id in RESERVED_INTERNAL_AUTH_ORG_IDS
            or not isolate_database
        ):
            definition = resolve_internal_environment_org()
            expected_ref = (
                definition.expected_project_ref
                if definition and definition.auth_org_id == auth_org_id
                else None
            )
            bind_organization_to_platform_project(
                db,
                auth_org_id=auth_org_id,
                expected_project_ref=expected_ref,
            )
        elif isolate_on_this_env:
            _queue_tenant_provision(auth_org_id)
        else:
            raise ValueError(
                'Customer organizations require a dedicated database schema, '
                'but tenant isolation is not configured on this environment.'
            )
        logger.info(
            'organization_created',
            auth_org_id=auth_org_id,
            name=display_name,
            kind=resolved_kind,
        )
        return organization

    if _string(name) and organization.name != display_name:
        organization.name = display_name
        organization.updated_at = _now()
        logger.info('organization_renamed', auth_org_id=auth_org_id, name=display_name)
    return organization


def create_organization(
    db: Session,
    *,
    name: str,
    creator_user_id: str,
    creator_email: str | None = None,
    creator_name: str | None = None,
    kind: str | None = None,
    slug: str | None = None,
) -> Organization:
    """Create a brand-new organization and add the creator as Master User."""
    from app.core.roles import MASTER_USER
    from app.services.internal_org_service import (
        ORG_KIND_EXTERNAL,
        is_platform_super_admin_email,
        is_reserved_internal_org_name,
        is_reserved_internal_slug,
        seed_internal_environment_organization,
    )

    if is_reserved_internal_org_name(name) or is_reserved_internal_slug(slug):
        # Super-admin claiming "Development"/"Staging"/"Production" joins this
        # environment's seeded internal org instead of creating a lookalike.
        if is_platform_super_admin_email(creator_email):
            organization = seed_internal_environment_organization(db)
            if organization is None:
                raise ValueError(
                    'No seeded internal organization is configured for this environment.'
                )
            upsert_member(
                db,
                auth_org_id=organization.auth_org_id,
                auth_user_id=creator_user_id,
                role=MASTER_USER,
                email=creator_email,
                full_name=creator_name,
                status=MEMBER_STATUS_ACTIVE,
                org_name=organization.name,
            )
            try:
                db.commit()
                db.refresh(organization)
            except SQLAlchemyError as exc:
                db.rollback()
                logger.exception(
                    'internal_org_super_admin_join_failed',
                    auth_org_id=organization.auth_org_id,
                    error_type=type(exc).__name__,
                )
                raise
            logger.info(
                'internal_org_super_admin_joined',
                auth_org_id=organization.auth_org_id,
                email=str(creator_email or '').strip().lower(),
            )
            return organization

        raise ValueError(
            'Development, Staging, and Production are reserved internal accounts. '
            'Use the seeded internal organization for this environment.'
        )

    if not is_platform_super_admin_email(creator_email):
        from app.services.schema_tenant_provisioner import tenant_schema_name

        tenant_schema_name(name)

    auth_org_id = str(uuid4())
    organization = get_or_create_organization(
        db,
        auth_org_id=auth_org_id,
        name=name,
        kind=kind or ORG_KIND_EXTERNAL,
        slug=slug,
        isolate_database=not is_platform_super_admin_email(creator_email),
    )
    upsert_member(
        db,
        auth_org_id=auth_org_id,
        auth_user_id=creator_user_id,
        role=DEFAULT_CUSTOMER_CREATOR_ROLE,
        email=creator_email,
        full_name=creator_name,
        status=MEMBER_STATUS_ACTIVE,
    )
    try:
        db.commit()
        db.refresh(organization)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'organization_create_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )
        raise
    flush_pending_tenant_provisions()
    return organization


def upsert_member(
    db: Session,
    *,
    auth_org_id: str,
    auth_user_id: str,
    role: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
    status: str | None = None,
    org_name: str | None = None,
) -> OrganizationMember:
    """Ensure the org exists, then create or update the membership row."""
    auth_org_id = _string(auth_org_id)
    auth_user_id = _string(auth_user_id)
    if not auth_org_id or not auth_user_id:
        raise ValueError('auth_org_id and auth_user_id are required')

    get_or_create_organization(db, auth_org_id=auth_org_id, name=org_name)

    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == auth_org_id,
            OrganizationMember.auth_user_id == auth_user_id,
        )
        .one_or_none()
    )
    resolved_role = _string(role) or DEFAULT_INVITE_ROLE
    resolved_status = _string(status) or MEMBER_STATUS_ACTIVE
    if member is None:
        member = OrganizationMember(
            id=str(uuid4()),
            auth_org_id=auth_org_id,
            auth_user_id=auth_user_id,
            role=resolved_role,
            status=resolved_status,
            email=_string(email) or None,
            full_name=_string(full_name) or None,
            created_at=_now(),
            updated_at=_now(),
        )
        _stamp_permissions_for_role(db, member, resolved_role)
        db.add(member)
        return member

    changed = False
    role_changed = False
    if role is not None and member.role != resolved_role:
        member.role = resolved_role
        changed = True
        role_changed = True
    if status is not None and member.status != resolved_status:
        member.status = resolved_status
        changed = True
    if email is not None:
        next_email = _string(email) or None
        if member.email != next_email:
            member.email = next_email
            changed = True
    if full_name is not None:
        next_name = _string(full_name) or None
        if member.full_name != next_name:
            member.full_name = next_name
            changed = True
    if role_changed or member.permissions_granted is None:
        _stamp_permissions_for_role(db, member, member.role)
        changed = True
    if changed:
        member.updated_at = _now()
        from app.core.security import clear_membership_role_cache

        clear_membership_role_cache(user_id=auth_user_id, org_id=auth_org_id)
    return member


def _stamp_permissions_for_role(db: Session, member: OrganizationMember, role: str) -> None:
    """Populate permissions_granted JSON from the org's System role defaults."""
    from app.services.member_permissions_service import permissions_granted_payload
    from app.services.role_permissions_service import role_permission_defaults_for_ui

    granted = role_permission_defaults_for_ui(member.auth_org_id, role, db=db)
    member.permissions_granted = permissions_granted_payload(granted)


def set_member_role(
    db: Session,
    *,
    auth_org_id: str,
    auth_user_id: str,
    role: str,
) -> OrganizationMember | None:
    """Update the role for an existing member. Returns None when the member is missing."""
    auth_org_id = _string(auth_org_id)
    auth_user_id = _string(auth_user_id)
    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == auth_org_id,
            OrganizationMember.auth_user_id == auth_user_id,
        )
        .one_or_none()
    )
    if member is None:
        return None
    resolved_role = _string(role) or member.role
    if member.role != resolved_role:
        member.role = resolved_role
        member.updated_at = _now()
        _stamp_permissions_for_role(db, member, resolved_role)
        from app.core.security import clear_membership_role_cache

        clear_membership_role_cache(user_id=auth_user_id, org_id=auth_org_id)
    return member


def remove_member(
    db: Session,
    *,
    auth_org_id: str,
    auth_user_id: str,
) -> bool:
    auth_org_id = _string(auth_org_id)
    auth_user_id = _string(auth_user_id)
    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == auth_org_id,
            OrganizationMember.auth_user_id == auth_user_id,
        )
        .one_or_none()
    )
    if member is None:
        return False
    db.delete(member)
    from app.core.security import clear_membership_role_cache

    clear_membership_role_cache(user_id=auth_user_id, org_id=auth_org_id)
    return True


def delete_organization(db: Session, auth_org_id: str) -> bool:
    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == _string(auth_org_id))
        .one_or_none()
    )
    if organization is None:
        return False
    db.delete(organization)
    return True


def list_memberships_for_user(db: Session, user_id: str) -> list[OrganizationMember]:
    """Return every active organization membership for a Supabase Auth user id."""
    user_id = _string(user_id)
    if not user_id:
        return []
    return (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_user_id == user_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .order_by(OrganizationMember.created_at.asc())
        .all()
    )


def list_members(db: Session, auth_org_id: str) -> list[OrganizationMember]:
    auth_org_id = _string(auth_org_id)
    return (
        db.query(OrganizationMember)
        .filter(OrganizationMember.auth_org_id == auth_org_id)
        .order_by(OrganizationMember.full_name.asc(), OrganizationMember.email.asc())
        .all()
    )
