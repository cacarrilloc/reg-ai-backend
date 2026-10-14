"""Local organization membership management (no external IdP API calls).

Replaces the old Clerk Backend API client (``clerk_organization_service``).
Role assignment, member listing, and removal are now plain reads/writes
against ``organization_members``.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from app.core.roles import CHIEF_COMPLIANCE_OFFICER, NOMINATED_TEAM_MEMBER
from app.models.core import OrganizationMember
from app.services.org_lifecycle_service import (
    list_members as _list_members,
)
from app.services.org_lifecycle_service import (
    remove_member,
    set_member_role,
    upsert_member,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class MemberView:
    """Plain view of an organization member for API responses."""

    user_id: str
    email: str | None
    full_name: str | None
    role: str
    status: str


def assign_cco_role(db: Session, *, auth_org_id: str, cco_user_id: str) -> OrganizationMember:
    """Ensure the organization's designated CCO holds the CCO role locally."""
    return assign_role(db, auth_org_id=auth_org_id, user_id=cco_user_id, role=CHIEF_COMPLIANCE_OFFICER)


def assign_nominated_team_member_role(
    db: Session, *, auth_org_id: str, user_id: str
) -> OrganizationMember:
    """Add or promote a member to the nominated reviewer role."""
    return assign_role(db, auth_org_id=auth_org_id, user_id=user_id, role=NOMINATED_TEAM_MEMBER)


def assign_role(
    db: Session,
    *,
    auth_org_id: str,
    user_id: str,
    role: str,
    email: str | None = None,
    full_name: str | None = None,
) -> OrganizationMember:
    """Create the membership if missing, or update its role in place."""
    member = set_member_role(db, auth_org_id=auth_org_id, auth_user_id=user_id, role=role)
    if member is not None:
        return member
    return upsert_member(
        db,
        auth_org_id=auth_org_id,
        auth_user_id=user_id,
        role=role,
        email=email,
        full_name=full_name,
    )


def list_organization_members(db: Session, auth_org_id: str) -> list[MemberView]:
    """Return local organization members as plain view objects."""
    return [
        MemberView(
            user_id=row.auth_user_id,
            email=row.email,
            full_name=row.full_name,
            role=row.role,
            status=row.status,
        )
        for row in _list_members(db, auth_org_id)
    ]


def remove_organization_member(db: Session, *, auth_org_id: str, user_id: str) -> bool:
    return remove_member(db, auth_org_id=auth_org_id, auth_user_id=user_id)
