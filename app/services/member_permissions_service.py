"""Per-member System permission overrides stored on organization_members."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.permissions import SYSTEM_PERMISSION_KEYS
from app.core.roles import MASTER_USER, normalize_role
from app.core.security import CurrentUser
from app.models.core import OrganizationMember
from app.schemas.organizations import (
    MemberPermissionEntry,
    OrganizationMemberPermissionListResponse,
    OrganizationMemberPermissionResponse,
    OrganizationMemberPermissionsUpdate,
)
from app.services.internal_org_service import is_reserved_internal_auth_org_id
from app.services.org_lifecycle_service import MEMBER_STATUS_REMOVED
from app.services.role_permissions_service import (
    role_permission_defaults_for_ui,
)

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_permissions_granted(raw: object | None) -> list[str] | None:
    """Parse permissions_granted JSON into a sorted list of valid keys.

    Returns None when the column is empty/unset (treat as role defaults).
    An explicit empty list means no System permissions granted.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return None
    if not isinstance(raw, (list, tuple)):
        return None
    keys = {
        str(item).strip()
        for item in raw
        if isinstance(item, str) and item.strip() in SYSTEM_PERMISSION_KEYS
    }
    return sorted(keys)


def permissions_granted_payload(keys: set[str] | frozenset[str] | list[str]) -> list[str]:
    return sorted({key for key in keys if key in SYSTEM_PERMISSION_KEYS})


def resolve_member_permissions(
    auth_org_id: str,
    auth_user_id: str,
    *,
    db: Session | None = None,
) -> frozenset[str] | None:
    """Return stored member permissions_granted, or None to use role defaults."""
    if db is None:
        from app.db.session import SessionLocal

        if SessionLocal is None:
            return None
        session = SessionLocal()
        try:
            return _resolve_member_with_session(session, auth_org_id, auth_user_id)
        except Exception:
            return None
        finally:
            session.close()

    try:
        return _resolve_member_with_session(db, auth_org_id, auth_user_id)
    except Exception:
        return None


def _resolve_member_with_session(
    db: Session,
    auth_org_id: str,
    auth_user_id: str,
) -> frozenset[str] | None:
    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == auth_org_id,
            OrganizationMember.auth_user_id == auth_user_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .one_or_none()
    )
    if member is None:
        return None
    normalized = normalize_permissions_granted(member.permissions_granted)
    if normalized is None:
        return None
    return frozenset(normalized)


def stamp_member_permissions_from_role(
    member: OrganizationMember,
    *,
    db: Session,
    force: bool = False,
) -> None:
    """Write role-default permissions onto the member row when unset (or forced)."""
    if not force and normalize_permissions_granted(member.permissions_granted) is not None:
        return
    role = normalize_role(member.role)
    granted = role_permission_defaults_for_ui(member.auth_org_id, role, db=db)
    member.permissions_granted = permissions_granted_payload(granted)
    member.updated_at = _now()


def list_member_permissions(
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberPermissionListResponse:
    _require_member_permissions_viewer(user)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Member permissions are unavailable without a configured database',
        )

    members = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == user.org_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .order_by(OrganizationMember.full_name.asc(), OrganizationMember.email.asc())
        .all()
    )

    entries: list[MemberPermissionEntry] = []
    dirty = False
    for member in members:
        role = normalize_role(member.role)
        role_defaults = sorted(
            role_permission_defaults_for_ui(user.org_id, role, db=db)
        )
        stored = normalize_permissions_granted(member.permissions_granted)
        if stored is None:
            # Ensure the column is populated on first read.
            member.permissions_granted = role_defaults
            member.updated_at = _now()
            stored = role_defaults
            dirty = True
        is_custom = stored != role_defaults
        entries.append(
            MemberPermissionEntry(
                user_id=member.auth_user_id,
                email=member.email,
                full_name=member.full_name,
                role=role,
                permissions=stored,
                role_default_permissions=role_defaults,
                is_custom=is_custom,
            )
        )

    if dirty:
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception('member_permissions_backfill_failed', org_id=user.org_id)

    return OrganizationMemberPermissionListResponse(
        auth_org_id=user.org_id,
        members=entries,
        catalog_keys=sorted(SYSTEM_PERMISSION_KEYS),
    )


def update_member_permissions(
    target_user_id: str,
    payload: OrganizationMemberPermissionsUpdate,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberPermissionResponse:
    _require_member_permissions_editor(user)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Member permissions are unavailable without a configured database',
        )

    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == user.org_id,
            OrganizationMember.auth_user_id == target_user_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .one_or_none()
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Organization member not found',
        )

    permission_keys = permissions_granted_payload(payload.permissions)
    role = normalize_role(member.role)
    role_defaults = sorted(role_permission_defaults_for_ui(user.org_id, role, db=db))

    try:
        member.permissions_granted = permission_keys
        member.updated_at = _now()
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'member_permissions_update_failed',
            org_id=user.org_id,
            user_id=target_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update member permissions',
        ) from exc

    from app.core.security import clear_membership_role_cache

    clear_membership_role_cache(user_id=target_user_id, org_id=user.org_id)

    return OrganizationMemberPermissionResponse(
        auth_org_id=user.org_id,
        user_id=target_user_id,
        role=role,
        permissions=permission_keys,
        role_default_permissions=role_defaults,
        is_custom=permission_keys != role_defaults,
    )


def reset_member_permissions(
    target_user_id: str,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberPermissionResponse:
    """Reset permissions_granted JSON to the member's current role defaults."""
    _require_member_permissions_editor(user)
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Member permissions are unavailable without a configured database',
        )

    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == user.org_id,
            OrganizationMember.auth_user_id == target_user_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .one_or_none()
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Organization member not found',
        )

    role = normalize_role(member.role)
    role_defaults = sorted(role_permission_defaults_for_ui(user.org_id, role, db=db))

    try:
        member.permissions_granted = role_defaults
        member.updated_at = _now()
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'member_permissions_reset_failed',
            org_id=user.org_id,
            user_id=target_user_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to reset member permissions',
        ) from exc

    from app.core.security import clear_membership_role_cache

    clear_membership_role_cache(user_id=target_user_id, org_id=user.org_id)

    return OrganizationMemberPermissionResponse(
        auth_org_id=user.org_id,
        user_id=target_user_id,
        role=role,
        permissions=role_defaults,
        role_default_permissions=role_defaults,
        is_custom=False,
    )


def _is_master(user: CurrentUser) -> bool:
    return MASTER_USER in user.effective_roles() or normalize_role(user.role) == MASTER_USER


def _require_member_permissions_viewer(user: CurrentUser) -> None:
    if is_reserved_internal_auth_org_id(user.org_id):
        return
    if not _is_master(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can view member permissions',
        )


def _require_member_permissions_editor(user: CurrentUser) -> None:
    if is_reserved_internal_auth_org_id(user.org_id):
        return
    if not _is_master(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can manage member permissions',
        )
