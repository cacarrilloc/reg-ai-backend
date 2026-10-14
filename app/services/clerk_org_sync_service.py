"""Backward-compatible aliases for the pre-Supabase-Auth organization sync API.

Organization/membership lifecycle logic now lives in
``app.services.org_lifecycle_service``. This module re-exports the old
function names so existing imports (and webhook payload parsing helpers)
keep working while callers migrate.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.core import Organization, OrganizationMember
from app.services.org_lifecycle_service import (
    delete_organization as delete_organization_by_clerk_id,
)
from app.services.org_lifecycle_service import (
    flush_pending_tenant_provisions,
)
from app.services.org_lifecycle_service import (
    get_or_create_organization as upsert_organization_from_clerk,
)
from app.services.org_lifecycle_service import (
    remove_member as delete_organization_member,
)
from app.services.org_lifecycle_service import (
    upsert_member as upsert_organization_member,
)

__all__ = [
    'upsert_organization_from_clerk',
    'upsert_organization_member',
    'delete_organization_member',
    'delete_organization_by_clerk_id',
    'flush_pending_tenant_provisions',
    'handle_clerk_organization_event',
    'apply_clerk_organization_event',
]

logger = structlog.get_logger(__name__)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ''


def _membership_public_user(data: dict[str, Any]) -> dict[str, Any]:
    return _as_dict(data.get('public_user_data') or data.get('public_user'))


def handle_clerk_organization_event(db: Session, event_type: str, data: dict[str, Any]) -> str:
    """Apply a legacy Clerk organization / membership webhook payload.

    Retained for archival webhook replays only; the ``/webhooks/clerk``
    endpoint is deprecated and no longer calls this in normal operation.
    """
    if event_type in {'organization.created', 'organization.updated'}:
        auth_org_id = _string(data.get('id'))
        name = _string(data.get('name'))
        upsert_organization_from_clerk(db, auth_org_id=auth_org_id, name=name or None)
        return 'organization_upserted'

    if event_type == 'organization.deleted':
        deleted = delete_organization_by_clerk_id(db, _string(data.get('id')))
        return 'organization_deleted' if deleted else 'organization_missing'

    if event_type in {
        'organizationMembership.created',
        'organizationMembership.updated',
    }:
        organization = _as_dict(data.get('organization'))
        public_user = _membership_public_user(data)
        auth_org_id = _string(organization.get('id') or data.get('organization_id'))
        auth_user_id = _string(
            public_user.get('user_id')
            or data.get('user_id')
            or public_user.get('id')
        )
        email = _string(
            public_user.get('identifier')
            or public_user.get('email_address')
            or data.get('email_address')
        )
        first = _string(public_user.get('first_name'))
        last = _string(public_user.get('last_name'))
        full_name = ' '.join(part for part in (first, last) if part) or None
        upsert_organization_member(
            db,
            auth_org_id=auth_org_id,
            auth_user_id=auth_user_id,
            role=_string(data.get('role')) or None,
            email=email or None,
            full_name=full_name,
            org_name=_string(organization.get('name')) or None,
        )
        return 'membership_upserted'

    if event_type == 'organizationMembership.deleted':
        organization = _as_dict(data.get('organization'))
        public_user = _membership_public_user(data)
        auth_org_id = _string(organization.get('id') or data.get('organization_id'))
        auth_user_id = _string(
            public_user.get('user_id')
            or data.get('user_id')
            or public_user.get('id')
        )
        deleted = delete_organization_member(
            db,
            auth_org_id=auth_org_id,
            auth_user_id=auth_user_id,
        )
        return 'membership_deleted' if deleted else 'membership_missing'

    return 'ignored'


def apply_clerk_organization_event(db: Session, event_type: str, data: dict[str, Any]) -> str:
    """Handle event and commit. Retained for archival webhook replays."""
    try:
        action = handle_clerk_organization_event(db, event_type, data)
        if action != 'ignored':
            db.commit()
            flush_pending_tenant_provisions()
        return action
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        logger.exception(
            'legacy_clerk_organization_sync_failed',
            event_type=event_type,
            error_type=type(exc).__name__,
        )
        raise


# Kept for type-checkers / callers that import the models from this module.
__models__ = (Organization, OrganizationMember)
