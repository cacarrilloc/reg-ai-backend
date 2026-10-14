"""Per-organization System role defaults (4 roles × 9 boolean permissions)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.permissions import (
    DELETE_ORGANIZATION,
    MANAGE_BILLING,
    MANAGE_DOMAINS,
    MANAGE_ENTERPRISE_CONNECTIONS,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
    READ_BILLING,
    READ_DOMAINS,
    READ_MEMBERS,
    SYSTEM_PERMISSION_KEYS,
    default_permissions_for_role,
    editable_roles,
)
from app.core.roles import ALL_ROLES, MASTER_USER, normalize_role
from app.core.security import CurrentUser
from app.models.core import OrganizationRolePermission
from app.schemas.organizations import (
    OrganizationRolePermissionMatrixResponse,
    OrganizationRolePermissionsUpdate,
    RolePermissionMatrixEntry,
)
from app.services.internal_org_service import is_reserved_internal_auth_org_id

logger = structlog.get_logger(__name__)

# Maps System permission keys ↔ wide-table boolean columns.
PERMISSION_COLUMN_BY_KEY: dict[str, str] = {
    MANAGE_ORGANIZATION: 'manage_organization',
    DELETE_ORGANIZATION: 'delete_organization',
    READ_BILLING: 'read_billing',
    MANAGE_BILLING: 'manage_billing',
    READ_DOMAINS: 'read_domains',
    MANAGE_DOMAINS: 'manage_domains',
    MANAGE_ENTERPRISE_CONNECTIONS: 'manage_enterprise_connections',
    READ_MEMBERS: 'read_members',
    MANAGE_MEMBERS: 'manage_members',
}

PERMISSION_KEY_BY_COLUMN: dict[str, str] = {
    column: key for key, column in PERMISSION_COLUMN_BY_KEY.items()
}


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def ensure_organization_role_permissions(db: Session, auth_org_id: str) -> None:
    """Guarantee exactly one wide row per System role for this organization."""
    existing = {
        row.role
        for row in (
            db.query(OrganizationRolePermission)
            .filter(OrganizationRolePermission.auth_org_id == auth_org_id)
            .all()
        )
    }
    now = _now()
    for role in editable_roles():
        if role in existing:
            continue
        db.add(_row_from_permission_keys(auth_org_id, role, default_permissions_for_role(role), now))
    db.flush()


def _row_from_permission_keys(
    auth_org_id: str,
    role: str,
    keys: frozenset[str] | set[str] | list[str],
    updated_at: datetime | None = None,
) -> OrganizationRolePermission:
    granted = set(keys)
    return OrganizationRolePermission(
        auth_org_id=auth_org_id,
        role=role,
        manage_organization=MANAGE_ORGANIZATION in granted,
        delete_organization=DELETE_ORGANIZATION in granted,
        read_billing=READ_BILLING in granted,
        manage_billing=MANAGE_BILLING in granted,
        read_domains=READ_DOMAINS in granted,
        manage_domains=MANAGE_DOMAINS in granted,
        manage_enterprise_connections=MANAGE_ENTERPRISE_CONNECTIONS in granted,
        read_members=READ_MEMBERS in granted,
        manage_members=MANAGE_MEMBERS in granted,
        updated_at=updated_at or _now(),
    )


def _keys_from_row(row: OrganizationRolePermission) -> frozenset[str]:
    granted: set[str] = set()
    for column, key in PERMISSION_KEY_BY_COLUMN.items():
        if bool(getattr(row, column, False)):
            granted.add(key)
    return frozenset(granted)


def _apply_keys_to_row(row: OrganizationRolePermission, keys: set[str] | frozenset[str]) -> None:
    for key, column in PERMISSION_COLUMN_BY_KEY.items():
        setattr(row, column, key in keys)
    row.updated_at = _now()


def resolve_role_permissions(
    auth_org_id: str,
    role: str,
    *,
    db: Session | None = None,
) -> frozenset[str]:
    """Return effective System permissions for a role in an organization."""
    canonical = normalize_role(role)
    defaults = default_permissions_for_role(canonical)

    if db is None:
        from app.db.session import SessionLocal

        if SessionLocal is None:
            return defaults
        session = SessionLocal()
        try:
            return _resolve_with_session(session, auth_org_id, canonical, defaults)
        except Exception:
            return defaults
        finally:
            session.close()

    try:
        return _resolve_with_session(db, auth_org_id, canonical, defaults)
    except Exception:
        return defaults


def role_permission_defaults_for_ui(
    auth_org_id: str,
    role: str,
    *,
    db: Session | None = None,
) -> frozenset[str]:
    """Permissions shown for a role in UI (always from the wide DB row when present)."""
    return resolve_role_permissions(auth_org_id, role, db=db)


def _resolve_with_session(
    db: Session,
    auth_org_id: str,
    role: str,
    defaults: frozenset[str],
) -> frozenset[str]:
    ensure_organization_role_permissions(db, auth_org_id)
    row = (
        db.query(OrganizationRolePermission)
        .filter(
            OrganizationRolePermission.auth_org_id == auth_org_id,
            OrganizationRolePermission.role == role,
        )
        .one_or_none()
    )
    if row is None:
        return defaults
    return _keys_from_row(row)


def get_role_permission_matrix(
    user: CurrentUser,
    db: Session | None,
) -> OrganizationRolePermissionMatrixResponse:
    _require_member_permissions_viewer(user)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Role permissions are unavailable without a configured database',
        )

    try:
        ensure_organization_role_permissions(db, user.org_id)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception('role_permissions_ensure_failed', org_id=user.org_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to load role permissions',
        )

    roles: list[RolePermissionMatrixEntry] = []
    for role in editable_roles():
        granted = sorted(resolve_role_permissions(user.org_id, role, db=db))
        defaults = sorted(default_permissions_for_role(role))
        roles.append(
            RolePermissionMatrixEntry(
                role=role,
                permissions=granted,
                is_default=granted == defaults,
            )
        )
    return OrganizationRolePermissionMatrixResponse(
        auth_org_id=user.org_id,
        roles=roles,
        catalog_keys=sorted(SYSTEM_PERMISSION_KEYS),
    )


def update_role_permissions(
    payload: OrganizationRolePermissionsUpdate,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationRolePermissionMatrixResponse:
    _require_member_permissions_editor(user)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Role permissions are unavailable without a configured database',
        )

    role = normalize_role(payload.role)
    if role not in ALL_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Unknown role',
        )

    permission_keys = {
        key.strip()
        for key in payload.permissions
        if isinstance(key, str) and key.strip() in SYSTEM_PERMISSION_KEYS
    }
    previous_defaults = sorted(role_permission_defaults_for_ui(user.org_id, role, db=db))
    next_defaults = sorted(permission_keys)

    try:
        ensure_organization_role_permissions(db, user.org_id)
        row = (
            db.query(OrganizationRolePermission)
            .filter(
                OrganizationRolePermission.auth_org_id == user.org_id,
                OrganizationRolePermission.role == role,
            )
            .one_or_none()
        )
        if row is None:
            row = _row_from_permission_keys(user.org_id, role, permission_keys)
            db.add(row)
        else:
            _apply_keys_to_row(row, permission_keys)

        # Keep member rows in sync when they were still using the previous role defaults.
        from app.models.core import OrganizationMember
        from app.services.member_permissions_service import (
            normalize_permissions_granted,
            permissions_granted_payload,
        )
        from app.services.org_lifecycle_service import MEMBER_STATUS_REMOVED

        now = _now()
        members = (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.auth_org_id == user.org_id,
                OrganizationMember.role == role,
                OrganizationMember.status != MEMBER_STATUS_REMOVED,
            )
            .all()
        )
        previous_set = set(previous_defaults)
        for member in members:
            stored = normalize_permissions_granted(member.permissions_granted)
            if stored is None or set(stored) == previous_set:
                member.permissions_granted = permissions_granted_payload(next_defaults)
                member.updated_at = now
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception('role_permissions_update_failed', org_id=user.org_id, role=role)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update role permissions',
        ) from exc

    from app.core.security import clear_membership_role_cache

    clear_membership_role_cache(org_id=user.org_id)
    return get_role_permission_matrix(user, db)


def _is_master(user: CurrentUser) -> bool:
    return MASTER_USER in user.effective_roles() or normalize_role(user.role) == MASTER_USER


def _require_member_permissions_viewer(user: CurrentUser) -> None:
    if is_reserved_internal_auth_org_id(user.org_id):
        return
    if not _is_master(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can view Member Permissions',
        )


def _require_member_permissions_editor(user: CurrentUser) -> None:
    # External: Master only. Internal Dev/Staging/Production: any member can edit.
    if is_reserved_internal_auth_org_id(user.org_id):
        return
    if not _is_master(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can manage Member Permissions',
        )
