from __future__ import annotations

from uuid import uuid4

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.roles import CHIEF_COMPLIANCE_OFFICER
from app.core.security import CurrentUser
from app.models.core import Organization
from app.schemas.admin import ClientOrganizationCreate, ClientOrganizationResponse
from app.services.organization_membership_service import assign_cco_role

logger = structlog.get_logger(__name__)


def create_client_organization(
    payload: ClientOrganizationCreate,
    user: CurrentUser,
    db: Session | None,
) -> ClientOrganizationResponse:
    org_id = str(uuid4())

    if db is None:
        return ClientOrganizationResponse(
            id=org_id,
            name=payload.name,
            auth_org_id=payload.auth_org_id,
            cco_user_id=payload.cco_user_id,
            regulatory_scope=payload.regulatory_scope,
            approval_chain=payload.approval_chain,
            escalation_days=payload.escalation_days,
            created_by=user.user_id,
            persisted=False,
            cco_role=CHIEF_COMPLIANCE_OFFICER,
            role_assigned=False,
        )

    existing = db.query(Organization).filter(
        Organization.auth_org_id == payload.auth_org_id).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='A client organization already exists for that organization ID',
        )

    organization = Organization(
        id=org_id,
        auth_org_id=payload.auth_org_id,
        name=payload.name,
        cco_user_id=payload.cco_user_id,
        regulatory_scope=payload.regulatory_scope,
        approval_chain=payload.approval_chain,
        escalation_days=payload.escalation_days,
    )

    try:
        db.add(organization)
        db.commit()
        db.refresh(organization)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail='Client organization already exists') from exc

    role_assigned = assign_cco_role_for_client_org(payload, db)

    return ClientOrganizationResponse(
        id=organization.id,
        name=organization.name,
        auth_org_id=organization.auth_org_id,
        cco_user_id=organization.cco_user_id or '',
        regulatory_scope=organization.regulatory_scope,
        approval_chain=organization.approval_chain,
        escalation_days=organization.escalation_days,
        created_by=user.user_id,
        persisted=True,
        cco_role=CHIEF_COMPLIANCE_OFFICER,
        role_assigned=role_assigned,
    )


def assign_cco_role_for_client_org(payload: ClientOrganizationCreate, db: Session) -> bool:
    """Assign the Chief Compliance Officer role for a client organization locally.

    Returns True when the local membership role assignment succeeds; failures
    are logged and return False instead of raising, since this is best-effort
    bootstrapping alongside organization creation.
    """
    try:
        assign_cco_role(db, auth_org_id=payload.auth_org_id, cco_user_id=payload.cco_user_id)
        db.commit()
        return True
    except (SQLAlchemyError, ValueError) as exc:
        db.rollback()
        logger.warning(
            'cco_role_assignment_failed',
            auth_org_id=payload.auth_org_id,
            error_type=type(exc).__name__,
        )
        return False
