"""Organization creation, switching, and email-invite workflow.

Membership itself lives in ``organization_members``; this module adds the
customer-facing flows on top: create-a-new-org, list-my-orgs, switch the
active org (persisted into the Supabase Auth user's ``app_metadata``), and
invite/accept by email.
"""

from __future__ import annotations

import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import key_staging
from app.ai.settings_service import OrgAISettingsService, OwnKeyChoice
from app.ai.types import AIEncryptionUnavailable, AIError
from app.core.config import Settings, get_settings
from app.core.member_capabilities import DELETE_RESOURCES, member_can
from app.core.roles import (
    ALL_ROLES,
    DEFAULT_CUSTOMER_CREATOR_ROLE,
    MASTER_USER,
    normalize_role,
)
from app.core.security import CurrentUser
from app.db.session import TenantSchemaNotReady
from app.models.core import Organization, OrganizationInvite, OrganizationMember
from app.schemas.ai import OrganizationAISetup
from app.schemas.organizations import (
    MyOrganizationsResponse,
    MyOrganizationSummary,
    OrganizationCreateRequest,
    OrganizationCreateResponse,
    OrganizationInviteAcceptRequest,
    OrganizationInviteAcceptResponse,
    OrganizationInviteCancelResponse,
    OrganizationInviteCreate,
    OrganizationInviteListResponse,
    OrganizationInvitePreviewResponse,
    OrganizationInviteResponse,
    SwitchOrganizationRequest,
    SwitchOrganizationResponse,
)
from app.services.internal_org_service import is_platform_super_admin_email
from app.services.org_lifecycle_service import (
    MEMBER_STATUS_ACTIVE,
    create_organization,
    list_memberships_for_user,
    upsert_member,
)
from app.services.organization_logo_service import (
    migrate_legacy_storage_logo_if_needed,
    public_or_signed_logo_url,
)

logger = structlog.get_logger(__name__)

INVITE_TTL_DAYS = 7


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize DB/Python datetimes so naive and aware values can be compared."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _workflow_choices(setup: OrganizationAISetup) -> list[OwnKeyChoice]:
    """Analysis and redline jobs from the create-organization form."""
    if setup.option != 'own_key':
        return []
    return [
        OwnKeyChoice(
            provider=item.provider,
            model=item.model,
            kind='embedding' if item.role == 'embedding' else 'generation',
            fields={name: value.get_secret_value() for name, value in item.fields.items()},
            api_base=item.api_base,
            dimensions=item.dimensions,
            role=item.role,
        )
        for item in setup.workflows
    ]


def _merge_staged_fields(setup: OrganizationAISetup, actor_id: str) -> list[OwnKeyChoice]:
    """Fill keys the complete-setup call omitted with the keys staged before create."""
    staged = key_staging.load_staged_workflows(actor_id)
    choices = _workflow_choices(setup)
    if not choices:
        return [item for item in staged if item.fields and item.role in {'analysis', 'redline', 'embedding'}]
    merged: list[OwnKeyChoice] = []
    for choice in choices:
        if choice.fields:
            merged.append(choice)
            continue
        match = next((
            item for item in staged
            if item.fields and item.provider == choice.provider and item.model == choice.model
            and item.role == choice.role
        ), None)
        if match is None:
            continue
        merged.append(OwnKeyChoice(
            provider=choice.provider,
            model=choice.model,
            kind=choice.kind,
            fields=match.fields,
            api_base=choice.api_base if choice.api_base is not None else match.api_base,
            dimensions=choice.dimensions if choice.dimensions is not None else match.dimensions,
            role=choice.role,
        ))
    return merged


def _stage_registration_keys(user_id: str, choices: list[OwnKeyChoice]) -> None:
    key_staging.stage_workflows(user_id, choices)


def _own_key_error(exc: AIError) -> HTTPException:
    if isinstance(exc, AIEncryptionUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


async def verify_organization_ai_key(setup: OrganizationAISetup) -> None:
    """Test each registration key with its provider before the organization is created."""
    service = OrgAISettingsService()
    for own_key in _workflow_choices(setup):
        if not own_key.fields:
            continue
        try:
            service.validate_own_key(own_key)
            await service.verify_credential(
                None, own_key.provider, own_key.fields,
                api_base=own_key.api_base, model=own_key.model, kind=own_key.kind)
        except AIError as exc:
            raise _own_key_error(exc) from None


def _wait_for_ai_data_plane(auth_org_id: str) -> None:
    """Block until the org schema can store encrypted keys, or give up for the caller to retry."""
    from app.db.session import (
        SessionLocal,
        clear_tenant_binding_cache,
        prepare_org_data_plane,
        resolve_schema_for_org,
    )

    if SessionLocal is None or not auth_org_id:
        return
    from app.services.tenant_provision_dispatch import schedule_organization_tenant_provision

    delays = (0.0, 0.25, 0.5, 1.0, 2.0, 3.0)
    for delay in delays:
        if delay:
            time.sleep(delay)
        clear_tenant_binding_cache(auth_org_id)
        state = db_session_lookup_tenant_status(auth_org_id)
        if state is not None and state[0] != 'schema':
            return
        if state is None:
            schedule_organization_tenant_provision(auth_org_id)
            continue
        try:
            prepare_org_data_plane(auth_org_id)
        except TenantSchemaNotReady:
            schedule_organization_tenant_provision(auth_org_id)
            continue
        if resolve_schema_for_org(auth_org_id):
            return
        schedule_organization_tenant_provision(auth_org_id)


def db_session_lookup_tenant_status(auth_org_id: str) -> tuple[str, str] | None:
    from app.db.session import _lookup_tenant_status

    return _lookup_tenant_status(auth_org_id)


def _credentials_visible(service: OrgAISettingsService, auth_org_id: str, provider: str) -> bool:
    try:
        return provider in {item.provider for item in service._store.credential_statuses(auth_org_id)}
    except Exception:
        return False


def _apply_ai_setup(
    service: OrgAISettingsService,
    auth_org_id: str,
    setup: OrganizationAISetup,
    actor_id: str,
) -> bool:
    """Transfer staged keys into the org table, then configure analysis and redlines from those rows."""
    _wait_for_ai_data_plane(auth_org_id)
    try:
        if setup.option == 'own_key':
            choices = _merge_staged_fields(setup, actor_id)
            if not choices:
                logger.warning('organization_ai_setup_missing_staged_key', auth_org_id=auth_org_id)
                return False
            service.apply_workflow_choices(auth_org_id, choices, actor_id=actor_id)
            providers = {choice.provider for choice in choices if choice.fields}
            if key_staging.db_backed() and any(
                not _credentials_visible(service, auth_org_id, provider) for provider in providers
            ):
                logger.warning(
                    'organization_ai_setup_key_not_visible',
                    auth_org_id=auth_org_id,
                    provider=next(iter(providers), ''),
                )
                return False
            key_staging.discard_staged_key(actor_id)
        else:
            service.use_default_models(
                auth_org_id, actor_id=actor_id, accept_billing=setup.accept_reg_ai_billing)
    except (AIError, TenantSchemaNotReady, SQLAlchemyError) as exc:
        logger.warning(
            'organization_ai_setup_deferred',
            auth_org_id=auth_org_id,
            option=setup.option,
            error_type=type(exc).__name__,
        )
        return False
    return True


def complete_organization_ai_setup(
    setup: OrganizationAISetup,
    user: CurrentUser,
) -> bool:
    """Transfer the key stored before org create into the isolated org table."""
    choices = [choice for choice in _workflow_choices(setup) if choice.fields]
    if choices:
        try:
            service = OrgAISettingsService()
            for choice in choices:
                service.validate_own_key(choice)
            _stage_registration_keys(user.user_id, choices)
        except AIError as exc:
            raise _own_key_error(exc) from None
    dest_org_id = key_staging.staged_dest_org_id(user.user_id) or user.org_id
    if not dest_org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Select the organization before saving the AI model choice.',
        )
    return _apply_ai_setup(
        OrgAISettingsService(),
        dest_org_id,
        setup,
        user.user_id,
    )


def create_organization_for_user(
    payload: OrganizationCreateRequest,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationCreateResponse:
    """Create an org. An AI model choice (Reg-AI defaults or the org's own key) is mandatory."""
    if db is None:
        return OrganizationCreateResponse(
            id=str(uuid4()),
            auth_org_id='local-org',
            name=payload.name,
            slug=None,
            kind='external',
            persisted=False,
        )

    ai_service = OrgAISettingsService()
    pending = _workflow_choices(payload.ai_setup)
    own_key = pending[0] if pending else None
    if pending:
        try:
            for choice in pending:
                ai_service.validate_own_key(choice)
            with_fields = [choice for choice in pending if choice.fields]
            if with_fields:
                _stage_registration_keys(user.user_id, with_fields)
        except AIError as exc:
            raise _own_key_error(exc) from None

    try:
        organization = create_organization(
            db,
            name=payload.name,
            creator_user_id=user.user_id,
            creator_email=_claim_email(user),
            creator_name=None,
        )
    except ValueError as exc:
        if own_key is not None:
            key_staging.discard_staged_key(user.user_id)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    from app.core.roles import MASTER_USER
    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    active_role = (
        MASTER_USER
        if is_reserved_internal_auth_org_id(organization.auth_org_id)
        else DEFAULT_CUSTOMER_CREATOR_ROLE
    )
    update_supabase_active_org(
        user.user_id,
        organization.auth_org_id,
        get_settings(),
        role=active_role,
    )
    if own_key is not None:
        key_staging.bind_staged_org(user.user_id, organization.auth_org_id)
    ai_setup_applied = _apply_ai_setup(
        ai_service,
        organization.auth_org_id,
        payload.ai_setup,
        user.user_id,
    )
    return OrganizationCreateResponse(
        id=organization.id,
        auth_org_id=organization.auth_org_id,
        name=organization.name,
        slug=organization.slug,
        kind=getattr(organization, 'kind', None) or 'external',
        persisted=True,
        ai_setup_applied=ai_setup_applied,
    )


def ensure_platform_super_admin_environment_workspace(
    user: CurrentUser,
    db: Session,
    *,
    settings: Settings | None = None,
) -> Organization | None:
    """Ensure Haider is on this ENVIRONMENT's internal org (Development/Staging/Production).

    - Seeds / renames the env internal org so ``name`` matches ENVIRONMENT mapping
    - Upserts master membership
    - Retires accidental UUID-named external bootstrap orgs (membership removed,
      tenant marked failed so provisioning does not retry)
    - Sets Supabase ``active_org_id`` only when current active is missing or a
      UUID bootstrap org
    """
    email = _claim_email(user) or _profile_email(user, db)
    if not is_platform_super_admin_email(email):
        return None
    if not user.user_id:
        return None

    from app.services.internal_org_service import (
        is_bootstrap_uuid_organization,
        seed_internal_environment_organization,
    )
    from app.services.org_lifecycle_service import MEMBER_STATUS_REMOVED
    from app.services.organization_tenant_service import mark_tenant_failed

    settings = settings or get_settings()
    organization = seed_internal_environment_organization(db, settings=settings)
    if organization is None:
        logger.warning(
            'platform_super_admin_env_org_missing',
            environment=settings.environment,
            email=email,
        )
        return None

    upsert_member(
        db,
        auth_org_id=organization.auth_org_id,
        auth_user_id=user.user_id,
        role=MASTER_USER,
        email=email,
        status=MEMBER_STATUS_ACTIVE,
        org_name=organization.name,
    )

    memberships = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_user_id == user.user_id,
            OrganizationMember.status != MEMBER_STATUS_REMOVED,
        )
        .all()
    )
    retired_ids: list[str] = []
    for member in memberships:
        if member.auth_org_id == organization.auth_org_id:
            continue
        candidate = (
            db.query(Organization)
            .filter(Organization.auth_org_id == member.auth_org_id)
            .one_or_none()
        )
        if not is_bootstrap_uuid_organization(candidate):
            continue
        member.status = MEMBER_STATUS_REMOVED
        mark_tenant_failed(
            db,
            auth_org_id=member.auth_org_id,
            error=(
                'Retired accidental UUID-named bootstrap org for platform '
                'super-admin; use environment internal organization instead. '
                'Delete the dedicated Supabase project manually if still present.'
            ),
        )
        retired_ids.append(member.auth_org_id)

    try:
        db.commit()
        db.refresh(organization)
    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            'platform_super_admin_env_workspace_failed',
            email=email,
            auth_org_id=organization.auth_org_id,
        )
        raise

    active_org_id = str(user.org_id or '').strip()
    should_activate = not active_org_id or active_org_id in retired_ids
    if not should_activate and active_org_id:
        active_org = (
            db.query(Organization)
            .filter(Organization.auth_org_id == active_org_id)
            .one_or_none()
        )
        should_activate = is_bootstrap_uuid_organization(active_org)

    if should_activate:
        update_supabase_active_org(
            user.user_id,
            organization.auth_org_id,
            settings,
            role=MASTER_USER,
        )

    logger.info(
        'platform_super_admin_env_workspace_ensured',
        email=email,
        auth_org_id=organization.auth_org_id,
        name=organization.name,
        environment=settings.environment,
        retired_bootstrap_orgs=retired_ids,
        activated=should_activate,
    )
    return organization


def list_my_organizations(user: CurrentUser, db: Session | None) -> MyOrganizationsResponse:
    if db is None:
        return MyOrganizationsResponse(organizations=[])

    try:
        ensure_platform_super_admin_environment_workspace(user, db)
    except Exception:
        logger.exception('platform_super_admin_env_workspace_failed', user_id=user.user_id)

    memberships = list_memberships_for_user(db, user.user_id)
    if not memberships:
        return MyOrganizationsResponse(organizations=[])

    org_ids = [m.auth_org_id for m in memberships]
    organizations = {
        org.auth_org_id: org
        for org in db.query(Organization).filter(Organization.auth_org_id.in_(org_ids)).all()
    }
    for organization in organizations.values():
        try:
            migrate_legacy_storage_logo_if_needed(db, organization)
        except Exception:
            logger.warning(
                'organization_logo_migrate_failed',
                auth_org_id=organization.auth_org_id,
            )

    summaries: list[MyOrganizationSummary] = []
    for member in memberships:
        org = organizations.get(member.auth_org_id)
        logo_url = getattr(org, 'logo_url', None) if org is not None else None
        try:
            logo_display_url = public_or_signed_logo_url(logo_url)
        except Exception:
            logo_display_url = None
        summaries.append(
            MyOrganizationSummary(
                auth_org_id=member.auth_org_id,
                name=org.name if org is not None else member.auth_org_id,
                role=member.role,
                status=member.status,
                kind=(getattr(org, 'kind', None) if org is not None else None) or 'external',
                logo_url=logo_url,
                logo_display_url=logo_display_url,
            )
        )
    return MyOrganizationsResponse(organizations=summaries)


def switch_active_organization(
    payload: SwitchOrganizationRequest,
    user: CurrentUser,
    db: Session | None,
) -> SwitchOrganizationResponse:
    if db is None:
        return SwitchOrganizationResponse(auth_org_id=payload.auth_org_id, updated=False)

    member = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.auth_org_id == payload.auth_org_id,
            OrganizationMember.auth_user_id == user.user_id,
        )
        .one_or_none()
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='You are not a member of that organization',
        )

    updated = update_supabase_active_org(
        user.user_id,
        payload.auth_org_id,
        get_settings(),
        role=member.role,
    )
    return SwitchOrganizationResponse(auth_org_id=payload.auth_org_id, updated=updated)


def update_supabase_active_org(
    user_id: str,
    auth_org_id: str,
    settings: Settings,
    *,
    role: str | None = None,
) -> bool:
    """Persist active org (+ optional IAM role) into Supabase Auth ``app_metadata``.

    Best-effort: returns False (without raising) when Supabase Admin API
    credentials are not configured, since local/dev environments run without
    a live Supabase project.
    """
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return False

    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}"
    headers = {
        'apikey': settings.supabase_service_role_key,
        'Authorization': f'Bearer {settings.supabase_service_role_key}',
        'Content-Type': 'application/json',
    }
    app_metadata: dict[str, str] = {'active_org_id': auth_org_id, 'org_id': auth_org_id}
    if role:
        app_metadata['role'] = normalize_role(role)
    try:
        response = httpx.put(
            url,
            headers=headers,
            json={'app_metadata': app_metadata},
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning(
            'supabase_active_org_update_failed',
            user_id=user_id,
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
        )
        return False


def clear_supabase_active_org(user_id: str, settings: Settings) -> bool:
    """Clear the active organization from Supabase Auth ``app_metadata``."""
    if not settings.supabase_url or not settings.supabase_service_role_key:
        return False

    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}"
    headers = {
        'apikey': settings.supabase_service_role_key,
        'Authorization': f'Bearer {settings.supabase_service_role_key}',
        'Content-Type': 'application/json',
    }
    try:
        response = httpx.put(
            url,
            headers=headers,
            json={
                'app_metadata': {
                    'active_org_id': '',
                    'org_id': '',
                    'role': '',
                }
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning(
            'supabase_active_org_clear_failed',
            user_id=user_id,
            error_type=type(exc).__name__,
        )
        return False


def create_organization_invite(
    payload: OrganizationInviteCreate,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationInviteResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Invites are unavailable without a configured database',
        )

    role = normalize_role(payload.role)
    # All orgs may invite Master, Product Manager, CCO, or Nominated.
    allowed_roles = ALL_ROLES
    if role not in allowed_roles:
        detail = (
            'Invite role must be one of: master_user, product_manager, '
            'chief_compliance_officer, nominated_team_member'
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )
    invitee_name = (payload.invitee_name or '').strip() or None
    organization_name = _organization_name(db, user.org_id)
    invite = OrganizationInvite(
        id=str(uuid4()),
        auth_org_id=user.org_id,
        email=payload.email.strip().lower(),
        invitee_name=invitee_name,
        role=role,
        token=secrets.token_urlsafe(32),
        expires_at=_now() + timedelta(days=INVITE_TTL_DAYS),
        invited_by=user.user_id,
        status='pending',
        created_at=_now(),
    )
    try:
        db.add(invite)
        db.commit()
        db.refresh(invite)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to create the organization invite',
        ) from exc

    try:
        _send_invite_email(invite, organization_name=organization_name)
    except HTTPException:
        # Do not leave a "pending" invite that was never emailed.
        try:
            db.delete(invite)
            db.commit()
        except SQLAlchemyError:
            db.rollback()
        raise
    return _invite_response(invite)


def list_organization_invites(user: CurrentUser, db: Session | None) -> OrganizationInviteListResponse:
    if db is None:
        return OrganizationInviteListResponse(
            invites=[],
            can_cancel_invites=member_can(user.role, DELETE_RESOURCES),
        )

    rows = (
        db.query(OrganizationInvite)
        .filter(OrganizationInvite.auth_org_id == user.org_id)
        .order_by(OrganizationInvite.created_at.desc())
        .all()
    )
    now = _now()
    dirty = False
    pending: list[OrganizationInvite] = []
    for row in rows:
        if row.status == 'pending' and _as_naive_utc(row.expires_at) < now:
            row.status = 'expired'
            dirty = True
            continue
        if row.status == 'pending':
            pending.append(row)

    if dirty:
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()

    return OrganizationInviteListResponse(
        invites=[_invite_response(row, include_token=False) for row in pending],
        can_cancel_invites=member_can(user.role, DELETE_RESOURCES),
    )


def cancel_organization_invite(
    invite_id: str,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationInviteCancelResponse:
    """Permanently delete a pending invite. Master and CCO only."""
    if not member_can(user.role, DELETE_RESOURCES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master and CCO users can delete pending invitations',
        )
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Invites are unavailable without a configured database',
        )

    invite = (
        db.query(OrganizationInvite)
        .filter(
            OrganizationInvite.id == invite_id,
            OrganizationInvite.auth_org_id == user.org_id,
        )
        .one_or_none()
    )
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Invitation not found',
        )
    if invite.status != 'pending':
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'Only pending invitations can be deleted (status={invite.status})',
        )
    if _as_naive_utc(invite.expires_at) < _now():
        invite.status = 'expired'
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='This invitation has expired',
        )

    email = invite.email
    invite_pk = invite.id
    try:
        db.delete(invite)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to delete the invitation',
        ) from exc

    return OrganizationInviteCancelResponse(id=invite_pk, email=email, cancelled=True)


def preview_organization_invite(
    token: str,
    db: Session | None,
) -> OrganizationInvitePreviewResponse:
    """Public invite preview used to route unsigned users to sign-up/sign-in."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Invites are unavailable without a configured database',
        )
    cleaned = (token or '').strip()
    if len(cleaned) < 8:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid invite token')

    invite = (
        db.query(OrganizationInvite)
        .filter(OrganizationInvite.token == cleaned)
        .one_or_none()
    )
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Invite not found')

    expired = invite.status == 'expired' or _as_naive_utc(invite.expires_at) < _now()
    status_value = 'expired' if expired and invite.status == 'pending' else invite.status
    return OrganizationInvitePreviewResponse(
        email=invite.email,
        role=invite.role,
        organization_name=_organization_name(db, invite.auth_org_id),
        status=status_value,
        expired=expired,
    )


def accept_organization_invite(
    payload: OrganizationInviteAcceptRequest,
    user: CurrentUser,
    db: Session | None,
) -> OrganizationInviteAcceptResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Invites are unavailable without a configured database',
        )

    invite = (
        db.query(OrganizationInvite)
        .filter(OrganizationInvite.token == payload.token)
        .one_or_none()
    )
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Invite not found')

    # Idempotent accept: if this user already joined via the same invite, succeed.
    if invite.status == 'accepted':
        existing = (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.auth_org_id == invite.auth_org_id,
                OrganizationMember.auth_user_id == user.user_id,
            )
            .one_or_none()
        )
        if existing is not None:
            update_supabase_active_org(
                user.user_id,
                invite.auth_org_id,
                get_settings(),
                role=invite.role,
            )
            return OrganizationInviteAcceptResponse(
                auth_org_id=invite.auth_org_id, role=invite.role, joined=True
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Invite is no longer pending',
        )

    if invite.status != 'pending':
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Invite is no longer pending')
    if _as_naive_utc(invite.expires_at) < _now():
        invite.status = 'expired'
        db.commit()
        raise HTTPException(status_code=status.HTTP_410_GONE, detail='Invite has expired')

    user_email = (_resolve_user_email(user, db) or '').strip().lower()
    invite_email = (invite.email or '').strip().lower()
    if not user_email or user_email != invite_email:
        signed_in_as = user_email or 'an account without a detectable email'
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f'This invite was sent to {invite.email}. '
                f'You are signed in as {signed_in_as}. '
                'Sign in with that email address to accept.'
            ),
        )

    upsert_member(
        db,
        auth_org_id=invite.auth_org_id,
        auth_user_id=user.user_id,
        role=invite.role,
        email=invite.email,
        full_name=_resolve_member_full_name(
            user,
            invite.email,
            db=db,
            invitee_name=getattr(invite, 'invitee_name', None),
        ),
    )
    invite.status = 'accepted'
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to accept the organization invite',
        ) from exc

    update_supabase_active_org(
        user.user_id,
        invite.auth_org_id,
        get_settings(),
        role=invite.role,
    )

    return OrganizationInviteAcceptResponse(
        auth_org_id=invite.auth_org_id, role=invite.role, joined=True
    )


def _organization_name(db: Session, auth_org_id: str) -> str | None:
    org = (
        db.query(Organization)
        .filter(Organization.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if org is None:
        return None
    name = (org.name or '').strip()
    return name or None


def _role_label(role: str) -> str:
    labels = {
        'master_user': 'Master User',
        'product_manager': 'Product Manager',
        'chief_compliance_officer': 'Chief Compliance Officer',
        'nominated_team_member': 'Nominated Team Member',
    }
    return labels.get(role, role.replace('_', ' ').title())


def _build_invite_email_html(
    *,
    invite: OrganizationInvite,
    join_url: str,
    organization_name: str | None,
) -> str:
    from app.services.notification_service import html_text

    role_label = _role_label(invite.role)
    invitee = (getattr(invite, 'invitee_name', None) or '').strip()
    greeting = f'Hi {html_text(invitee)},' if invitee else 'Hello,'
    org_label = html_text(organization_name or 'a Reg-AI organization')
    safe_role = html_text(role_label)
    safe_email = html_text(invite.email)
    safe_url = html_text(join_url)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Reg-AI invitation</title>
</head>
<body
  style="margin:0;padding:0;background:#eef2f7;color:#0f172a;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;"
>
  <table
    role="presentation"
    width="100%"
    cellspacing="0"
    cellpadding="0"
    style="background:#eef2f7;padding:32px 16px;"
  >
    <tr>
      <td align="center">
        <table
          role="presentation"
          width="100%"
          cellspacing="0"
          cellpadding="0"
          style="max-width:560px;background:#ffffff;border-radius:16px;
          overflow:hidden;box-shadow:0 10px 30px rgba(15,23,42,0.08);"
        >
          <tr>
            <td
              style="background:linear-gradient(135deg,#0b1f3a 0%,#12315a 55%,#1d4f91 100%);
              padding:28px 32px;"
            >
              <p
                style="margin:0;font-size:12px;letter-spacing:0.18em;
                text-transform:uppercase;color:#93c5fd;font-weight:600;"
              >Reg AI</p>
              <h1
                style="margin:10px 0 0;font-size:24px;line-height:1.25;
                color:#ffffff;font-weight:700;"
              >You're invited</h1>
              <p
                style="margin:8px 0 0;font-size:14px;line-height:1.5;color:#dbeafe;"
              >Join your team on the Rule Change Tracker</p>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px;font-size:16px;line-height:1.6;">{greeting}</p>
              <p style="margin:0 0 20px;font-size:15px;line-height:1.6;color:#334155;">
                You've been invited to join
                <strong style="color:#0f172a;">{org_label}</strong> on Reg-AI.
              </p>
              <table
                role="presentation"
                width="100%"
                cellspacing="0"
                cellpadding="0"
                style="margin:0 0 24px;background:#f8fafc;
                border:1px solid #e2e8f0;border-radius:12px;"
              >
                <tr>
                  <td style="padding:16px 18px;">
                    <p
                      style="margin:0 0 6px;font-size:11px;letter-spacing:0.12em;
                      text-transform:uppercase;color:#64748b;font-weight:600;"
                    >Assigned role</p>
                    <p style="margin:0;">
                      <span
                        style="display:inline-block;background:#0b1f3a;color:#ffffff;
                        border-radius:999px;padding:6px 12px;font-size:13px;font-weight:600;"
                      >
                        {safe_role}
                      </span>
                    </p>
                  </td>
                </tr>
              </table>
              <p style="margin:0 0 24px;font-size:15px;line-height:1.6;color:#334155;">
                Sign in with
                <strong style="color:#0f172a;">{safe_email}</strong>
                to join. You do not need to create a new organization.
              </p>
              <table
                role="presentation"
                cellspacing="0"
                cellpadding="0"
                style="margin:0 0 20px;"
              >
                <tr>
                  <td align="center" bgcolor="#0b1f3a" style="border-radius:10px;">
                    <a
                      href="{safe_url}"
                      style="display:inline-block;padding:14px 28px;font-size:15px;
                      font-weight:600;color:#ffffff;text-decoration:none;"
                    >
                      Accept invitation
                    </a>
                  </td>
                </tr>
              </table>
              <p style="margin:0;font-size:12px;line-height:1.6;color:#94a3b8;">
                If the button doesn't work, paste this link into your browser:<br />
                <a href="{safe_url}" style="color:#1d4f91;word-break:break-all;">{safe_url}</a>
              </p>
            </td>
          </tr>
          <tr>
            <td
              style="padding:18px 32px 24px;border-top:1px solid #e2e8f0;background:#f8fafc;"
            >
              <p style="margin:0;font-size:12px;line-height:1.5;color:#94a3b8;">
                This invitation was sent by Reg-AI.
                If you weren't expecting it, you can ignore this email.
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>
"""


def _send_invite_email(
    invite: OrganizationInvite,
    *,
    organization_name: str | None = None,
) -> None:
    from app.services.notification_service import send_resend_email

    settings = get_settings()
    # Use /join (not /invite) — many ad blockers drop URLs containing "invite".
    join_url = f"{settings.frontend_url.rstrip('/')}/join?token={invite.token}"
    role_label = _role_label(invite.role)
    org_bit = f' ({organization_name})' if organization_name else ''
    result = send_resend_email(
        to_email=invite.email,
        subject=f"You're invited to Reg-AI{org_bit} as {role_label}",
        html=_build_invite_email_html(
            invite=invite,
            join_url=join_url,
            organization_name=organization_name,
        ),
    )
    send_status = result.get('status')
    if send_status == 'sent':
        return

    reason = result.get('error') or result.get('reason') or 'Unknown email delivery error'
    logger.warning(
        'organization_invite_email_failed',
        invite_id=invite.id,
        email=invite.email,
        status=send_status,
        reason=reason,
    )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f'Invite was not emailed to {invite.email}: {reason}',
    )


def _invite_response(
    invite: OrganizationInvite,
    *,
    include_token: bool = True,
) -> OrganizationInviteResponse:
    return OrganizationInviteResponse(
        id=invite.id,
        auth_org_id=invite.auth_org_id,
        email=invite.email,
        invitee_name=getattr(invite, 'invitee_name', None),
        role=invite.role,
        token=invite.token if include_token else None,
        expires_at=invite.expires_at,
        status=invite.status,
        created_at=invite.created_at,
    )


def _is_master(user: CurrentUser) -> bool:
    return MASTER_USER in user.effective_roles() or normalize_role(user.role) == MASTER_USER


def _claim_email(user: CurrentUser) -> str | None:
    """Extract email from common Supabase / JWT claim locations."""
    claims = user.claims or {}
    raw_user_metadata = claims.get('user_metadata')
    raw_app_metadata = claims.get('app_metadata')
    user_metadata: dict[str, Any] = (
        raw_user_metadata if isinstance(raw_user_metadata, dict) else {}
    )
    app_metadata: dict[str, Any] = (
        raw_app_metadata if isinstance(raw_app_metadata, dict) else {}
    )
    candidates = [
        claims.get('email'),
        user_metadata.get('email'),
        app_metadata.get('email'),
        claims.get('preferred_email'),
        claims.get('user_email'),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip().lower()
        if text and '@' in text:
            return text
    return None


def _profile_email(user: CurrentUser, db: Session | None) -> str | None:
    if db is None or not user.user_id:
        return None
    try:
        from app.models.core import UserProfile

        profile = (
            db.query(UserProfile)
            .filter(UserProfile.auth_user_id == user.user_id)
            .one_or_none()
        )
    except SQLAlchemyError:
        return None
    if profile is None:
        return None
    email = getattr(profile, 'email', None)
    text = str(email).strip().lower() if email else ''
    return text if text and '@' in text else None


def _supabase_admin_email(user_id: str) -> str | None:
    """Fetch the Auth user's email via Supabase Admin API when JWT omits it."""
    settings = get_settings()
    if not user_id or not settings.supabase_url or not settings.supabase_service_role_key:
        return None
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}"
    headers = {
        'apikey': settings.supabase_service_role_key,
        'Authorization': f'Bearer {settings.supabase_service_role_key}',
    }
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    email = payload.get('email')
    text = str(email).strip().lower() if email else ''
    return text if text and '@' in text else None


def _resolve_user_email(user: CurrentUser, db: Session | None = None) -> str | None:
    """Best-effort signed-in email for invite matching."""
    return (
        _claim_email(user)
        or _profile_email(user, db)
        or _supabase_admin_email(user.user_id)
    )


def _email_local_part(email: str | None) -> str | None:
    value = (email or '').strip()
    if not value or '@' not in value:
        return None
    local = value.split('@', 1)[0].strip()
    return local or None


def _claim_display_name(user: CurrentUser) -> str | None:
    """Best-effort display name from JWT / Auth metadata."""
    claims = user.claims or {}
    raw_user_metadata = claims.get('user_metadata')
    user_metadata: dict[str, Any] = (
        raw_user_metadata if isinstance(raw_user_metadata, dict) else {}
    )

    candidates: list[object] = [
        claims.get('full_name'),
        claims.get('name'),
        claims.get('display_name'),
        user_metadata.get('full_name'),
        user_metadata.get('name'),
        user_metadata.get('display_name'),
        user_metadata.get('username'),
        claims.get('preferred_username'),
    ]
    first = user_metadata.get('first_name') or claims.get('first_name') or claims.get('given_name')
    last = user_metadata.get('last_name') or claims.get('last_name') or claims.get('family_name')
    if first or last:
        candidates.insert(
            0,
            ' '.join(str(part).strip() for part in (first, last) if part and str(part).strip()),
        )

    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text
    return None


def _profile_username(user: CurrentUser, db: Session | None) -> str | None:
    if db is None or not user.user_id:
        return None
    try:
        from app.models.core import UserProfile

        profile = (
            db.query(UserProfile)
            .filter(UserProfile.auth_user_id == user.user_id)
            .one_or_none()
        )
    except SQLAlchemyError:
        return None
    if profile is None:
        return None
    username = getattr(profile, 'username', None)
    text = str(username).strip() if username else ''
    return text or None


def _resolve_member_full_name(
    user: CurrentUser,
    email: str | None,
    *,
    db: Session | None = None,
    invitee_name: str | None = None,
) -> str:
    """Resolve a non-empty membership display name.

    Order: invitee_name → JWT/Auth metadata → user_profiles.username →
    email local-part → email → user id.
    """
    invited = (invitee_name or '').strip()
    return (
        invited
        or _claim_display_name(user)
        or _profile_username(user, db)
        or _email_local_part(email)
        or _email_local_part(_claim_email(user))
        or (email or '').strip()
        or user.user_id
    )


# Default membership role for customers who create an organization.
__default_creator_role__ = DEFAULT_CUSTOMER_CREATOR_ROLE
