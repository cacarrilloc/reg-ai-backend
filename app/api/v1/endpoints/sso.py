"""Enterprise SSO: login policy, JIT sync, and self-serve org IdP setup."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.member_capabilities import require_delete_resources
from app.core.permissions import MANAGE_ENTERPRISE_CONNECTIONS
from app.core.security import CurrentUser, get_current_user, require_permission
from app.db.session import get_platform_db
from app.services.organization_sso_service import (
    connection_payload,
    delete_org_sso_connection,
    get_connection_for_org,
    jit_sync_membership,
    login_policy_for_email,
    upsert_org_sso_connection,
)
from app.services.supabase_sso_admin import saml_sp_info

router = APIRouter(tags=['sso'])


class SsoLoginPolicyRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)


class SsoLoginPolicyResponse(BaseModel):
    domain: str | None = None
    sso_required: bool = False
    sso_available: bool = False
    auth_org_id: str | None = None


class SsoJitSyncRequest(BaseModel):
    groups: list[str] | None = None


class SsoJitSyncResponse(BaseModel):
    provisioned: bool
    auth_org_id: str | None = None
    role: str | None = None
    groups: list[str] = Field(default_factory=list)
    reason: str | None = None


class SsoConnectionUpsertRequest(BaseModel):
    domains: list[str] = Field(..., min_length=1)
    metadata_url: str | None = Field(default=None, max_length=2000)
    metadata_xml: str | None = Field(default=None, max_length=500_000)
    sso_only: bool = False
    jit_enabled: bool = True
    default_role: str = 'nominated_team_member'
    group_role_mappings: dict[str, str] = Field(default_factory=dict)
    attribute_groups_key: str = 'groups'


class SsoConnectionResponse(BaseModel):
    id: str
    auth_org_id: str
    domains: list[str]
    supabase_provider_id: str | None = None
    metadata_url: str | None = None
    sso_only: bool
    jit_enabled: bool
    default_role: str
    group_role_mappings: dict[str, str]
    attribute_groups_key: str
    status: str
    last_error: str | None = None
    sp: dict[str, str] | None = None
    updated_at: str | None = None


class SsoSpInfoResponse(BaseModel):
    entity_id: str
    acs_url: str
    metadata_url: str
    sso_enabled: bool


def _require_db(db: Session | None) -> Session:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    return db


def _require_org(user: CurrentUser) -> str:
    org_id = (user.org_id or '').strip()
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Active organization required',
        )
    return org_id


@router.post('/auth/sso/login-policy', response_model=SsoLoginPolicyResponse)
def post_sso_login_policy(
    payload: SsoLoginPolicyRequest,
    db: Session | None = Depends(get_platform_db),
) -> SsoLoginPolicyResponse:
    """Public: does this email belong to an SSO-only / SSO-available domain?"""
    session = _require_db(db)
    result = login_policy_for_email(session, payload.email)
    return SsoLoginPolicyResponse(**result)


@router.post('/auth/sso/jit-sync', response_model=SsoJitSyncResponse)
def post_sso_jit_sync(
    payload: SsoJitSyncRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> SsoJitSyncResponse:
    """After SSO / OAuth sign-in, JIT-provision org membership from domain + groups."""
    session = _require_db(db)
    email = None
    full_name = None
    claims = user.claims or {}
    if isinstance(claims.get('email'), str):
        email = claims['email']
    raw_user_metadata = claims.get('user_metadata')
    user_metadata: dict[str, object] = (
        raw_user_metadata if isinstance(raw_user_metadata, dict) else {}
    )
    if email is None:
        metadata_email = user_metadata.get('email')
        if isinstance(metadata_email, str):
            email = metadata_email
    for key in ('full_name', 'name'):
        metadata_value = user_metadata.get(key)
        if isinstance(metadata_value, str) and metadata_value.strip():
            full_name = metadata_value.strip()
            break
        claim_value = claims.get(key)
        if isinstance(claim_value, str) and claim_value.strip():
            full_name = claim_value.strip()
            break

    result = jit_sync_membership(
        session,
        auth_user_id=user.user_id,
        email=email,
        full_name=full_name,
        claims=claims,
        explicit_groups=payload.groups,
    )
    return SsoJitSyncResponse(
        provisioned=bool(result.get('provisioned')),
        auth_org_id=result.get('auth_org_id'),
        role=result.get('role'),
        groups=list(result.get('groups') or []),
        reason=result.get('reason'),
    )


@router.get(
    '/organizations/current/sso',
    response_model=SsoConnectionResponse | None,
)
def get_current_org_sso(
    user: CurrentUser = Depends(require_permission(MANAGE_ENTERPRISE_CONNECTIONS)),
    db: Session | None = Depends(get_platform_db),
) -> SsoConnectionResponse | None:
    session = _require_db(db)
    org_id = _require_org(user)
    connection = get_connection_for_org(session, org_id)
    if connection is None:
        return None
    return SsoConnectionResponse(**connection_payload(session, connection, get_settings()))


@router.put('/organizations/current/sso', response_model=SsoConnectionResponse)
def put_current_org_sso(
    payload: SsoConnectionUpsertRequest,
    user: CurrentUser = Depends(require_permission(MANAGE_ENTERPRISE_CONNECTIONS)),
    db: Session | None = Depends(get_platform_db),
) -> SsoConnectionResponse:
    session = _require_db(db)
    org_id = _require_org(user)
    settings = get_settings()
    try:
        connection = upsert_org_sso_connection(
            session,
            auth_org_id=org_id,
            domains=payload.domains,
            metadata_url=payload.metadata_url,
            metadata_xml=payload.metadata_xml,
            sso_only=payload.sso_only,
            jit_enabled=payload.jit_enabled,
            default_role=payload.default_role,
            group_role_mappings=payload.group_role_mappings,
            attribute_groups_key=payload.attribute_groups_key,
            settings=settings,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return SsoConnectionResponse(**connection_payload(session, connection, settings))


@router.delete('/organizations/current/sso', status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_current_org_sso(
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> Response:
    if not user.has_permission(MANAGE_ENTERPRISE_CONNECTIONS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient permission',
        )
    session = _require_db(db)
    org_id = _require_org(user)
    delete_org_sso_connection(session, auth_org_id=org_id, settings=get_settings())
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get('/organizations/current/sso/sp-info', response_model=SsoSpInfoResponse)
def get_current_org_sso_sp_info(
    user: CurrentUser = Depends(require_permission(MANAGE_ENTERPRISE_CONNECTIONS)),
) -> SsoSpInfoResponse:
    del user  # permission gate only
    settings = get_settings()
    if not settings.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SUPABASE_URL is not configured',
        )
    info = saml_sp_info(settings)
    return SsoSpInfoResponse(
        entity_id=info['entity_id'],
        acs_url=info['acs_url'],
        metadata_url=info['metadata_url'],
        sso_enabled=bool(settings.sso_enabled),
    )
