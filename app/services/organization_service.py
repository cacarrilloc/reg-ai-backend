from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import get_settings
from app.core.member_capabilities import DELETE_RESOURCES, member_can
from app.core.permissions import MANAGE_MEMBERS
from app.core.roles import NOMINATED_TEAM_MEMBER
from app.core.security import CurrentUser
from app.models.core import Organization
from app.schemas.organizations import (
    ApprovalChainUpdate,
    AssignedReviewerResponse,
    AssignedReviewerUpdate,
    NominatedTeamMemberAssign,
    OrganizationApprovalChainResponse,
    OrganizationLeaveResponse,
    OrganizationMemberListResponse,
    OrganizationMemberRemoveResponse,
    OrganizationMemberResponse,
    OrganizationMemberRoleResponse,
    OrganizationMembersSyncResponse,
    OrganizationSettingsResponse,
    OrganizationSettingsUpdate,
    RegulatoryScopeUpdate,
)
from app.services.org_lifecycle_service import get_or_create_organization, upsert_member
from app.services.organization_invite_service import clear_supabase_active_org
from app.services.organization_membership_service import (
    assign_nominated_team_member_role,
    remove_organization_member,
)
from app.services.organization_membership_service import (
    list_organization_members as list_local_organization_members,
)
from app.services.regulatory_monitoring_service import CFTC_SOURCE, FINRA_SOURCE, SEC_SOURCE

logger = structlog.get_logger(__name__)

# Org Settings selectable sources. NFA scraping may still exist in the platform,
# but it is not offered in Organization profile & regulatory scope for now.
ORGANIZATION_SCOPE_SOURCES = frozenset({SEC_SOURCE, FINRA_SOURCE, CFTC_SOURCE})

DEFAULT_REGULATORY_SCOPE: dict[str, Any] = {
    'sources': sorted(ORGANIZATION_SCOPE_SOURCES),
    'jurisdictions': ['US'],
    'products': [],
}


@dataclass(frozen=True)
class OrganizationMemberContact:
    """Emailable organization member for org-wide notifications."""

    user_id: str
    email: str
    full_name: str


def _user_email(user: CurrentUser, db: Session | None) -> str | None:
    """Resolve the caller's email from JWT claims or the local profile."""
    from app.services.organization_invite_service import _claim_email, _profile_email

    return _claim_email(user) or _profile_email(user, db)


def update_approval_chain(
    payload: ApprovalChainUpdate,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationApprovalChainResponse:
    settings = get_settings()
    if db is None:
        return OrganizationApprovalChainResponse(
            id='local-org',
            auth_org_id=user.org_id,
            name='Local organization',
            approval_chain=payload,
            escalation_days=payload.escalation_days or 7,
            sla_reminder_days=settings.sla_reminder_days,
            persisted=False,
        )

    organization = db.query(Organization).filter(
        Organization.auth_org_id == user.org_id).one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Create the client organization before configuring its approval chain',
        )

    incoming = payload.model_dump(mode='json')
    existing = dict(organization.approval_chain or {})
    # Settings UI may omit operational contacts; keep any previously saved reviewer/CCO.
    for key in (
        'assigned_reviewer_user_id',
        'assigned_reviewer_name',
        'assigned_reviewer_email',
        'cco_user_id',
        'cco_name',
        'cco_email',
    ):
        if incoming.get(key) is None and existing.get(key) is not None:
            incoming[key] = existing.get(key)
    organization.approval_chain = incoming
    flag_modified(organization, 'approval_chain')
    if payload.escalation_days is not None:
        organization.escalation_days = payload.escalation_days
    if incoming.get('cco_user_id'):
        organization.cco_user_id = str(incoming['cco_user_id']).strip() or None
    organization.updated_at = datetime.now(UTC).replace(tzinfo=None)

    try:
        db.commit()
        db.refresh(organization)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update the organization approval chain',
        ) from exc

    return OrganizationApprovalChainResponse(
        id=organization.id,
        auth_org_id=organization.auth_org_id,
        name=organization.name,
        approval_chain=ApprovalChainUpdate.model_validate(
            organization.approval_chain),
        escalation_days=organization.escalation_days,
        sla_reminder_days=settings.sla_reminder_days,
        updated_at=organization.updated_at,
        persisted=True,
    )


def get_organization_settings(
    user: CurrentUser,
    db: Session | None,
    *,
    org_name: str | None = None,
) -> OrganizationSettingsResponse:
    """Return org settings, bootstrapping a DB row when it is missing."""
    settings = get_settings()
    if db is None:
        return OrganizationSettingsResponse(
            id='local-org',
            auth_org_id=user.org_id,
            name=org_name or 'Local organization',
            cco_user_id=None,
            cco_name=None,
            cco_email=None,
            regulatory_scope=dict(DEFAULT_REGULATORY_SCOPE),
            available_sources=sorted(ORGANIZATION_SCOPE_SOURCES),
            escalation_days=7,
            sla_reminder_days=settings.sla_reminder_days,
            approval_chain=ApprovalChainUpdate(),
            persisted=False,
            bootstrapped=False,
        )

    organization, bootstrapped = ensure_organization_row(
        db,
        auth_org_id=user.org_id,
        name=org_name,
        auth_user_id=user.user_id or None,
        role=user.role,
        email=_user_email(user, db),
    )
    return _settings_response(db, organization, bootstrapped=bootstrapped)


def update_organization_settings(
    payload: OrganizationSettingsUpdate,
    user: CurrentUser,
    db: Session | None,
    *,
    org_name: str | None = None,
) -> OrganizationSettingsResponse:
    """Persist regulatory scope, CCO contact, and escalation settings."""
    settings = get_settings()
    if db is None:
        scope = (
            payload.regulatory_scope.model_dump(mode='json')
            if payload.regulatory_scope is not None
            else dict(DEFAULT_REGULATORY_SCOPE)
        )
        return OrganizationSettingsResponse(
            id='local-org',
            auth_org_id=user.org_id,
            name=payload.name or org_name or 'Local organization',
            cco_user_id=payload.cco_user_id,
            cco_name=payload.cco_name,
            cco_email=payload.cco_email,
            regulatory_scope=normalize_regulatory_scope(scope),
            available_sources=sorted(ORGANIZATION_SCOPE_SOURCES),
            escalation_days=payload.escalation_days or 7,
            sla_reminder_days=settings.sla_reminder_days,
            approval_chain=ApprovalChainUpdate(
                cco_user_id=payload.cco_user_id,
                cco_name=payload.cco_name,
                cco_email=payload.cco_email,
            ),
            persisted=False,
            bootstrapped=False,
        )

    organization, bootstrapped = ensure_organization_row(
        db,
        auth_org_id=user.org_id,
        name=org_name or payload.name,
        auth_user_id=user.user_id or None,
        role=user.role,
        email=_user_email(user, db),
    )

    if payload.name and payload.name.strip():
        organization.name = payload.name.strip()
    if payload.escalation_days is not None:
        organization.escalation_days = payload.escalation_days
    if payload.regulatory_scope is not None:
        organization.regulatory_scope = normalize_regulatory_scope(
            payload.regulatory_scope.model_dump(mode='json')
        )
        flag_modified(organization, 'regulatory_scope')

    chain = dict(organization.approval_chain or {})
    if payload.cco_user_id is not None:
        cco_user_id = payload.cco_user_id.strip() or None
        organization.cco_user_id = cco_user_id
        chain['cco_user_id'] = cco_user_id
    if payload.cco_name is not None:
        chain['cco_name'] = payload.cco_name.strip() or None
    if payload.cco_email is not None:
        chain['cco_email'] = payload.cco_email.strip() or None
    organization.approval_chain = chain
    flag_modified(organization, 'approval_chain')
    organization.updated_at = datetime.now(UTC).replace(tzinfo=None)

    try:
        db.commit()
        db.refresh(organization)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update organization settings',
        ) from exc

    return _settings_response(db, organization, bootstrapped=bootstrapped)


def ensure_organization_row(
    db: Session,
    *,
    auth_org_id: str,
    name: str | None = None,
    auth_user_id: str | None = None,
    role: str | None = None,
    email: str | None = None,
    full_name: str | None = None,
) -> tuple[Organization, bool]:
    """Return the org profile, creating it and the calling member if missing."""
    from app.services.internal_org_service import (
        is_bootstrap_uuid_organization,
        is_platform_super_admin_email,
        is_uuid_string,
    )
    from app.services.org_lifecycle_service import flush_pending_tenant_provisions

    # Platform super-admin must not bootstrap a UUID-named external workspace.
    if is_platform_super_admin_email(email) and auth_user_id:
        display_probe = (name or '').strip() or None
        existing_probe = (
            db.query(Organization)
            .filter(Organization.auth_org_id == auth_org_id)
            .one_or_none()
        )
        redirect_bootstrap = False
        if is_bootstrap_uuid_organization(existing_probe):
            redirect_bootstrap = True
        elif existing_probe is None and is_uuid_string(auth_org_id):
            if display_probe is None or is_uuid_string(display_probe):
                redirect_bootstrap = True
        if redirect_bootstrap:
            from app.core.security import CurrentUser
            from app.services.organization_invite_service import (
                ensure_platform_super_admin_environment_workspace,
            )

            synthetic = CurrentUser(
                user_id=auth_user_id,
                org_id=auth_org_id,
                role=role or 'master_user',
                claims={'email': email},
            )
            env_org = ensure_platform_super_admin_environment_workspace(synthetic, db)
            if env_org is not None:
                return env_org, False

    display_name = (name or '').strip() or None
    existing = (
        db.query(Organization)
        .filter(Organization.auth_org_id == auth_org_id)
        .one_or_none()
    )
    bootstrapped = existing is None
    try:
        organization = get_or_create_organization(
            db,
            auth_org_id=auth_org_id,
            name=display_name,
        )
        if auth_user_id:
            upsert_member(
                db,
                auth_org_id=auth_org_id,
                auth_user_id=auth_user_id,
                role=role,
                email=email,
                full_name=full_name,
                org_name=organization.name,
            )
        if db.new or db.dirty or db.deleted:
            db.commit()
            db.refresh(organization)
    except (ValueError, SQLAlchemyError) as exc:
        db.rollback()
        logger.exception(
            'organization_row_bootstrap_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to create the organization settings row',
        ) from exc
    if bootstrapped:
        from app.services.tenant_provision_dispatch import (
            schedule_organization_tenant_provision,
        )

        flush_pending_tenant_provisions()
        schedule_organization_tenant_provision(auth_org_id)
    return organization, bootstrapped


def normalize_regulatory_scope(
    scope: dict[str, Any] | RegulatoryScopeUpdate | None,
    *,
    require_sources: bool = True,
) -> dict[str, Any]:
    """Normalize scope JSON and keep only known regulator sources."""
    if isinstance(scope, RegulatoryScopeUpdate):
        data = scope.model_dump(mode='json')
    elif isinstance(scope, dict):
        data = dict(scope)
    else:
        data = dict(DEFAULT_REGULATORY_SCOPE)

    raw_sources = data.get('sources') or []
    if not isinstance(raw_sources, list):
        raw_sources = []
    sources = sorted(
        {
            str(source).strip().upper()
            for source in raw_sources
            if str(source).strip().upper() in ORGANIZATION_SCOPE_SOURCES
        }
    )
    if not sources:
        if require_sources:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='Select at least one regulatory source (SEC, FINRA, or CFTC).',
            )
        sources = list(DEFAULT_REGULATORY_SCOPE['sources'])

    jurisdictions = data.get('jurisdictions') or ['US']
    if not isinstance(jurisdictions, list):
        jurisdictions = ['US']
    jurisdictions = [
        str(item).strip()
        for item in jurisdictions
        if str(item).strip()
    ] or ['US']

    products = data.get('products') or []
    if not isinstance(products, list):
        products = []
    products = [str(item).strip() for item in products if str(item).strip()]

    return {
        'sources': sources,
        'jurisdictions': jurisdictions,
        'products': products,
    }


def _settings_response(
    db: Session | None,
    organization: Organization,
    *,
    bootstrapped: bool,
) -> OrganizationSettingsResponse:
    from app.services.organization_logo_service import (
        migrate_legacy_storage_logo_if_needed,
        public_or_signed_logo_url,
    )

    if db is not None:
        migrate_legacy_storage_logo_if_needed(db, organization)

    settings = get_settings()
    chain = ApprovalChainUpdate.model_validate(organization.approval_chain or {})
    logo_url = getattr(organization, 'logo_url', None)
    return OrganizationSettingsResponse(
        id=organization.id,
        auth_org_id=organization.auth_org_id,
        name=organization.name,
        logo_url=logo_url,
        logo_display_url=public_or_signed_logo_url(logo_url),
        cco_user_id=organization.cco_user_id or chain.cco_user_id,
        cco_name=chain.cco_name,
        cco_email=chain.cco_email,
        regulatory_scope=normalize_regulatory_scope(
            organization.regulatory_scope,
            require_sources=False,
        ),
        available_sources=sorted(ORGANIZATION_SCOPE_SOURCES),
        escalation_days=organization.escalation_days or 7,
        sla_reminder_days=settings.sla_reminder_days,
        approval_chain=chain,
        updated_at=organization.updated_at,
        persisted=True,
        bootstrapped=bootstrapped,
    )


def get_approval_chain(
    user: CurrentUser,
    db: Session | None,
) -> OrganizationApprovalChainResponse:
    settings = get_settings()
    if db is None:
        return OrganizationApprovalChainResponse(
            id='local-org',
            auth_org_id=user.org_id,
            name='Local organization',
            approval_chain=ApprovalChainUpdate(),
            escalation_days=7,
            sla_reminder_days=settings.sla_reminder_days,
            persisted=False,
        )

    organization = db.query(Organization).filter(
        Organization.auth_org_id == user.org_id).one_or_none()
    if organization is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail='Organization not found')

    return OrganizationApprovalChainResponse(
        id=organization.id,
        auth_org_id=organization.auth_org_id,
        name=organization.name,
        approval_chain=ApprovalChainUpdate.model_validate(
            organization.approval_chain or {}),
        escalation_days=organization.escalation_days,
        sla_reminder_days=settings.sla_reminder_days,
        updated_at=organization.updated_at,
        persisted=True,
    )


def assign_nominated_team_member(
    payload: NominatedTeamMemberAssign,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberRoleResponse:
    """Give a member of the active client organization the reviewer role."""
    if db is None:
        return OrganizationMemberRoleResponse(
            auth_org_id=user.org_id,
            user_id=payload.user_id,
            role=NOMINATED_TEAM_MEMBER,
            assigned=False,
        )

    organization = db.query(Organization).filter(
        Organization.auth_org_id == user.org_id).one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Create the client organization before assigning team-member roles',
        )

    assign_nominated_team_member_role(
        db, auth_org_id=organization.auth_org_id, user_id=payload.user_id
    )
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to assign the nominated team member role',
        ) from exc
    return OrganizationMemberRoleResponse(
        auth_org_id=organization.auth_org_id,
        user_id=payload.user_id,
        role=NOMINATED_TEAM_MEMBER,
        assigned=True,
    )


def list_organization_member_contacts(
    auth_org_id: str,
    db: Session | None = None,
) -> list[OrganizationMemberContact]:
    """Return organization members that have an email address on file."""
    auth_org_id = auth_org_id.strip()
    if not auth_org_id:
        return []

    if db is not None:
        return _member_contacts_from_db(db, auth_org_id)

    from app.db.session import platform_session_scope

    try:
        with platform_session_scope(commit=False) as session:
            return _member_contacts_from_db(session, auth_org_id)
    except RuntimeError:
        return []


def _member_contacts_from_db(db: Session, auth_org_id: str) -> list[OrganizationMemberContact]:
    contacts: list[OrganizationMemberContact] = []
    seen_emails: set[str] = set()
    for member in list_local_organization_members(db, auth_org_id):
        email = (member.email or '').strip()
        if not email:
            continue
        normalized = email.lower()
        if normalized in seen_emails:
            continue
        seen_emails.add(normalized)
        contacts.append(
            OrganizationMemberContact(
                user_id=member.user_id or normalized,
                email=email,
                full_name=member.full_name or email,
            )
        )
    contacts.sort(key=lambda item: item.full_name.lower())
    return contacts


def organization_notification_recipients(
    auth_org_id: str,
    organization: Organization | None = None,
    db: Session | None = None,
) -> list[OrganizationMemberContact]:
    """Recipients for org-wide review emails (decisions, reupload warnings).

    Prefers every local organization member with an email. Falls back to the
    configured CCO / assigned reviewer when no members have an email on file.
    """
    contacts = list_organization_member_contacts(auth_org_id, db)
    if contacts:
        return contacts

    fallback: list[OrganizationMemberContact] = []
    chain = organization.approval_chain if organization and organization.approval_chain else {}
    for email_key, name_key, user_key in (
        ('cco_email', 'cco_name', 'cco_user_id'),
        ('assigned_reviewer_email', 'assigned_reviewer_name', 'assigned_reviewer_user_id'),
    ):
        email = _optional_str(chain.get(email_key))
        if not email:
            continue
        if any(item.email.lower() == email.lower() for item in fallback):
            continue
        fallback.append(
            OrganizationMemberContact(
                user_id=_optional_str(chain.get(user_key)) or email.lower(),
                email=email,
                full_name=_optional_str(chain.get(name_key)) or email,
            )
        )
    return fallback


def sync_current_organization_members(
    user: CurrentUser,
    db: Session | None,
    *,
    org_name: str | None = None,
) -> OrganizationMembersSyncResponse:
    """Return the current member count for the active organization.

    Organization membership is now sourced entirely from ``organization_members``
    (no external IdP to pull from), so this is a compatibility no-op that just
    reports the current total.
    """
    from app.models.core import OrganizationMember as OrganizationMemberRow

    if db is None:
        return OrganizationMembersSyncResponse(
            auth_org_id=user.org_id,
            synced=0,
            total=0,
        )

    organization, _ = ensure_organization_row(
        db,
        auth_org_id=user.org_id,
        name=org_name,
        auth_user_id=user.user_id or None,
        role=user.role,
        email=_user_email(user, db),
    )
    total = (
        db.query(OrganizationMemberRow)
        .filter(OrganizationMemberRow.auth_org_id == organization.auth_org_id)
        .count()
    )
    return OrganizationMembersSyncResponse(
        auth_org_id=organization.auth_org_id,
        synced=0,
        total=total,
    )


def list_organization_members(
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberListResponse:
    """Return organization members mirrored locally in ``organization_members``."""
    if db is None:
        return OrganizationMemberListResponse(
            members=[],
            total=0,
            current_user_id=user.user_id,
            can_manage_members=user.has_permission(MANAGE_MEMBERS),
        )

    organization, _ = ensure_organization_row(
        db,
        auth_org_id=user.org_id,
        auth_user_id=user.user_id or None,
        role=user.role,
        email=_user_email(user, db),
    )

    members = [
        OrganizationMemberResponse(
            user_id=member.user_id,
            email=member.email,
            first_name=None,
            last_name=None,
            full_name=member.full_name,
            role=member.role,
            image_url=None,
        )
        for member in list_local_organization_members(db, organization.auth_org_id)
    ]
    return OrganizationMemberListResponse(
        members=members,
        total=len(members),
        current_user_id=user.user_id,
        can_manage_members=user.has_permission(MANAGE_MEMBERS),
    )


def remove_current_organization_member(
    user_id: str,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationMemberRemoveResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Members are unavailable without a configured database',
        )
    target_user_id = (user_id or '').strip()
    if not target_user_id:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='user_id is required')
    if target_user_id == user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Use leave organization to remove yourself',
        )
    if not member_can(user.role, DELETE_RESOURCES) or not user.has_permission(MANAGE_MEMBERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master and CCO users can remove members',
        )

    removed = remove_organization_member(
        db, auth_org_id=user.org_id, user_id=target_user_id
    )
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Member not found')
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to remove the member',
        ) from exc
    return OrganizationMemberRemoveResponse(
        auth_org_id=user.org_id,
        user_id=target_user_id,
        removed=True,
    )


def leave_current_organization(
    user: CurrentUser,
    db: Session | None,
) -> OrganizationLeaveResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Members are unavailable without a configured database',
        )
    if not user.user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Unknown user')

    left = remove_organization_member(
        db, auth_org_id=user.org_id, user_id=user.user_id
    )
    if not left:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='You are not a member of this organization',
        )
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to leave the organization',
        ) from exc

    clear_supabase_active_org(user.user_id, get_settings())
    return OrganizationLeaveResponse(auth_org_id=user.org_id, left=True)


def update_assigned_reviewer(
    payload: AssignedReviewerUpdate,
    user: CurrentUser,
    db: Session | None,
) -> AssignedReviewerResponse:
    """Persist the org-level assigned reviewer used by the Review Queue."""
    if db is None:
        return AssignedReviewerResponse(
            user_id=payload.user_id,
            email=payload.email,
            full_name=payload.full_name,
            persisted=False,
        )

    organization = db.query(Organization).filter(
        Organization.auth_org_id == user.org_id).one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Create the client organization before assigning a reviewer',
        )

    chain = dict(organization.approval_chain or {})
    chain['assigned_reviewer_user_id'] = payload.user_id
    chain['assigned_reviewer_email'] = (payload.email or '').strip() or None
    chain['assigned_reviewer_name'] = (payload.full_name or '').strip() or (
        (payload.email or '').strip() or payload.user_id
    )
    organization.approval_chain = chain
    flag_modified(organization, 'approval_chain')

    try:
        db.commit()
        db.refresh(organization)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to update the assigned reviewer',
        ) from exc

    return AssignedReviewerResponse(
        user_id=chain.get('assigned_reviewer_user_id'),
        email=chain.get('assigned_reviewer_email'),
        full_name=chain.get('assigned_reviewer_name'),
        persisted=True,
    )


def _optional_str(value: object) -> str | None:
    text = str(value).strip() if value is not None else ''
    return text or None
