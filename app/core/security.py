from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from threading import Lock
from time import monotonic
from typing import Any

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings, get_settings
from app.core.permissions import (
    SYSTEM_PERMISSION_KEYS,
    permissions_for_roles,
    roles_have_permission,
)
from app.core.roles import (
    ALL_ROLES,
    INTERNAL_ENV_FULL_ACCESS_ROLES,
    MASTER_USER,
    is_app_role,
    is_supabase_system_role,
    normalize_role,
)
from app.core.security_hardening import client_ip_from_request, report_failed_authentication

PUBLIC_PATHS = {
    '/',
    '/docs',
    '/favicon.ico',
    '/health',
    '/openapi.json',
    '/redoc',
    '/api/v1/health',
    '/api/v1/newsletter/publications',
    '/api/v1/newsletter/subscribe',
    '/api/v1/webhooks/stripe',
    '/api/v1/webhooks/supabase',
    '/sentry-debug',
}

# Avoid a platform DB round-trip on every authenticated request.
_MEMBERSHIP_ROLE_CACHE_TTL_SECONDS = 60.0
_membership_role_cache: dict[tuple[str, str], tuple[float, str, frozenset[str] | None]] = {}
_membership_role_cache_lock = Lock()


@dataclass(frozen=True)
class CurrentUser:
    """Represents the authenticated current user."""

    user_id: str
    org_id: str
    role: str = MASTER_USER
    roles: frozenset[str] = field(default_factory=frozenset)
    claims: dict[str, Any] | None = None
    aal: str | None = None
    # When set (org overrides / reserved-internal Master), takes precedence.
    system_permissions: frozenset[str] | None = None

    def has_any_role(self, allowed_roles: set[str]) -> bool:
        """Return whether any normalized role grants the requested access."""
        effective_roles = self.roles or {normalize_role(self.role)}
        return bool(effective_roles & allowed_roles)

    def effective_roles(self) -> frozenset[str]:
        """Normalized roles used for RBAC / System permission checks."""
        if self.roles:
            return frozenset(normalize_role(role) for role in self.roles)
        return frozenset({normalize_role(self.role)})

    def permissions(self) -> frozenset[str]:
        """System permission keys granted by the user's effective roles."""
        if self.system_permissions is not None:
            return self.system_permissions
        return permissions_for_roles(self.effective_roles())

    def has_permission(self, permission: str) -> bool:
        """Return whether any effective role grants a System permission."""
        key = (permission or '').strip()
        if key not in SYSTEM_PERMISSION_KEYS:
            return False
        if self.system_permissions is not None:
            return key in self.system_permissions
        return roles_have_permission(self.effective_roles(), permission)

    def has_any_permission(self, *permissions: str) -> bool:
        """Return whether any of the requested System permissions is granted."""
        return any(self.has_permission(permission) for permission in permissions)

    @property
    def has_aal2(self) -> bool:
        return (self.aal or '').lower() in {'aal2', '2'}


class SupabaseJWTVerifier:
    """Verifies Supabase Auth JWTs and converts claims to CurrentUser."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwk_client: Any | None = None

    def _get_jwk_client(self, jwks_url: str) -> Any:
        if self._jwk_client is None:
            from jwt import PyJWKClient

            self._jwk_client = PyJWKClient(
                jwks_url,
                cache_jwk_set=True,
                lifespan=300,
                timeout=5,
            )
        return self._jwk_client

    def verify_bearer_header(self, authorization: str | None) -> CurrentUser:
        if not authorization:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing bearer token')

        scheme, _, token = authorization.partition(' ')
        if scheme.lower() != 'bearer' or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Invalid Authorization header format',
            )

        return self.verify_token(token)

    def verify_token(self, token: str) -> CurrentUser:
        if not self.settings.supabase_auth_configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='Supabase Auth JWT verification is not configured',
            )

        try:
            import jwt
            from jwt.exceptions import InvalidTokenError, PyJWKClientConnectionError, PyJWTError
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='PyJWT is not installed',
            ) from exc

        jwks_url = self.settings.resolved_supabase_jwks_url
        if jwks_url is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='Supabase JWKS URL is not configured',
            )

        try:
            signing_key = self._get_jwk_client(jwks_url).get_signing_key_from_jwt(token)
            decode_kwargs: dict[str, Any] = {
                'key': signing_key.key,
                'algorithms': ['RS256', 'ES256'],
                'issuer': self.settings.supabase_jwt_issuer_values,
                'options': {'verify_aud': bool(self.settings.supabase_jwt_audience)},
            }
            if self.settings.supabase_jwt_audience:
                decode_kwargs['audience'] = self.settings.supabase_jwt_audience

            claims = jwt.decode(token, **decode_kwargs)
        except PyJWKClientConnectionError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='Unable to fetch Supabase JWKS',
            ) from exc
        except (InvalidTokenError, PyJWTError) as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail='Invalid Supabase Auth token',
            ) from exc

        return current_user_from_claims(claims, self.settings)


# Backward-compatible aliases while tests/services migrate.
ClerkJWTVerifier = SupabaseJWTVerifier


class AuthMiddleware(BaseHTTPMiddleware):  # pylint: disable=too-few-public-methods
    """Middleware that validates Supabase Auth for protected routes."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith('/api/v1/inngest'):
            return await call_next(request)

        settings = get_settings()
        authorization = request.headers.get('authorization')
        x_org_id = request.headers.get('x-org-id')
        if authorization:
            try:
                request.state.current_user = get_jwt_verifier().verify_bearer_header(authorization)
                from app.db.session import set_tenant_org_id

                set_tenant_org_id(request.state.current_user.org_id)
            except HTTPException as exc:
                if (
                    exc.status_code == status.HTTP_401_UNAUTHORIZED
                    and should_use_local_header_override(settings, x_org_id)
                ):
                    pass
                else:
                    if exc.status_code in {
                        status.HTTP_401_UNAUTHORIZED,
                        status.HTTP_403_FORBIDDEN,
                    }:
                        report_failed_authentication(
                            reason='invalid_bearer_token',
                            path=request.url.path,
                            client_ip=client_ip_from_request(request),
                            detail=str(exc.detail),
                        )
                    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)

        return await call_next(request)


ClerkAuthMiddleware = AuthMiddleware


def current_user_from_claims(claims: dict[str, Any], settings: Settings) -> CurrentUser:
    """Extract current user information from Supabase (or legacy) JWT claims."""
    app_metadata = claims.get('app_metadata') if isinstance(claims.get('app_metadata'), dict) else {}
    user_metadata = claims.get('user_metadata') if isinstance(claims.get('user_metadata'), dict) else {}

    org_id = first_claim_value(
        claims,
        [
            settings.auth_org_claim,
            'org_id',
            'orgId',
            'app_metadata.active_org_id',
            'app_metadata.org_id',
            'o.id',
            'organization.id',
        ],
    )
    if org_id is None and isinstance(app_metadata, dict):
        org_id = app_metadata.get('active_org_id') or app_metadata.get('org_id')

    # Prefer app/user metadata roles. Top-level ``role`` is last because Supabase
    # JWTs set it to the Postgres role ``authenticated``.
    role_paths = [
        settings.auth_role_claim,
        'app_metadata.role',
        'user_metadata.role',
        'org_role',
        'o.rol',
        'public_metadata.role',
        'metadata.role',
        'role',
    ]

    role = None
    for path in role_paths:
        value = claim_value(claims, path)
        if value is not None and not is_supabase_system_role(str(value)):
            role = value
            break
    if role is None and isinstance(app_metadata, dict):
        maybe = app_metadata.get('role')
        if maybe is not None and not is_supabase_system_role(str(maybe)):
            role = maybe
    if role is None and isinstance(user_metadata, dict):
        maybe = user_metadata.get('role')
        if maybe is not None and not is_supabase_system_role(str(maybe)):
            role = maybe

    roles = {
        normalize_role(str(value))
        for path in dict.fromkeys(role_paths)
        if (value := claim_value(claims, path)) is not None
        and not is_supabase_system_role(str(value))
    }
    if role is not None and not is_supabase_system_role(str(role)):
        normalized_role = normalize_role(str(role))
        roles.add(normalized_role)
    else:
        # No app role in JWT yet (custom access-token hook / app_metadata not set).
        # Leave an empty marker; membership hydration fills this later.
        normalized_role = next(iter(roles), '')
        if not normalized_role:
            normalized_role = 'authenticated'

    # Drop any residual system roles from the effective set.
    roles = {item for item in roles if is_app_role(item)}
    if is_app_role(normalized_role):
        roles.add(normalize_role(normalized_role))
    if not roles and is_app_role(normalized_role):
        roles = {normalize_role(normalized_role)}

    resolved_org_id = str(org_id).strip() if org_id is not None else ''
    if not resolved_org_id:
        # Users may authenticate before creating/selecting an organization.
        # App routes still require an active org via OrganizationGate.
        resolved_org_id = 'platform'

    aal = first_claim_value(claims, ['aal', 'app_metadata.aal'])
    return CurrentUser(
        user_id=str(claims.get('sub') or claims.get('user_id') or ''),
        org_id=resolved_org_id,
        role=normalize_role(normalized_role) if is_app_role(normalized_role) else normalized_role,
        roles=frozenset(roles),
        claims=claims,
        aal=str(aal) if aal is not None else None,
    )


def clear_membership_role_cache(
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Drop cached membership roles (all, one org, or one user/org pair)."""
    with _membership_role_cache_lock:
        if user_id and org_id:
            _membership_role_cache.pop((user_id, org_id), None)
            return
        if org_id:
            stale = [key for key in _membership_role_cache if key[1] == org_id]
            for key in stale:
                _membership_role_cache.pop(key, None)
            return
        if user_id:
            stale = [key for key in _membership_role_cache if key[0] == user_id]
            for key in stale:
                _membership_role_cache.pop(key, None)
            return
        _membership_role_cache.clear()


def _cached_membership(
    user_id: str, org_id: str
) -> tuple[str, frozenset[str] | None] | None:
    key = (user_id, org_id)
    now = monotonic()
    with _membership_role_cache_lock:
        item = _membership_role_cache.get(key)
        if item is None:
            return None
        expires_at, role, permissions = item
        if expires_at <= now:
            _membership_role_cache.pop(key, None)
            return None
        return role, permissions


def _store_membership(
    user_id: str,
    org_id: str,
    role: str,
    permissions: frozenset[str] | None,
) -> None:
    with _membership_role_cache_lock:
        _membership_role_cache[(user_id, org_id)] = (
            monotonic() + _MEMBERSHIP_ROLE_CACHE_TTL_SECONDS,
            role,
            permissions,
        )


def _apply_membership_role(
    user: CurrentUser,
    membership_role: str,
    *,
    system_permissions: frozenset[str] | None = None,
) -> CurrentUser:
    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    # Reserved Dev/Staging/Production: keep Master-equivalent role gates; System
    # permission keys still come from member/role overrides when configured.
    if is_reserved_internal_auth_org_id(user.org_id):
        return replace(
            user,
            role=membership_role,
            roles=frozenset(INTERNAL_ENV_FULL_ACCESS_ROLES),
            system_permissions=system_permissions
            if system_permissions is not None
            else frozenset(SYSTEM_PERMISSION_KEYS),
        )

    roles = set(user.roles)
    roles.add(membership_role)
    roles &= ALL_ROLES
    roles.add(membership_role)
    return replace(
        user,
        role=membership_role,
        roles=frozenset(roles),
        system_permissions=system_permissions,
    )


def enrich_user_from_membership(user: CurrentUser) -> CurrentUser:
    """Overlay org membership role from ``organization_members`` when available.

    JWT claims alone are incomplete until the custom access-token hook (or
    app_metadata write) runs. Membership is the source of truth for customer RBAC.

    Effective System permissions:
    1. Per-member overrides when present (full set replaces role defaults)
    2. Else org role matrix / defaults (internal reserved orgs default to full
       Master System permissions until a role is customized)

    Results are cached briefly to avoid a platform DB round-trip on every request.
    """
    if not user.user_id or not user.org_id or user.org_id == 'platform':
        return user

    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    cached = _cached_membership(user.user_id, user.org_id)
    if cached is not None:
        cached_role, cached_permissions = cached
        return _apply_membership_role(
            user,
            cached_role,
            system_permissions=cached_permissions,
        )

    # Reserved Dev/Staging/Production orgs: Master-equivalent gates + full System
    # permissions without a platform DB round-trip on every request.
    if is_reserved_internal_auth_org_id(user.org_id):
        primary = (
            normalize_role(user.role)
            if is_app_role(user.role)
            else MASTER_USER
        )
        return _apply_membership_role(user, primary)

    from app.db.session import SessionLocal
    from app.models.core import OrganizationMember

    if SessionLocal is None:
        return user

    db = SessionLocal()
    try:
        member = (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.auth_org_id == user.org_id,
                OrganizationMember.auth_user_id == user.user_id,
            )
            .one_or_none()
        )
        if member is None or not member.role:
            return user
        membership_role = normalize_role(member.role)
        if not is_app_role(membership_role):
            return user
        from app.services.member_permissions_service import normalize_permissions_granted

        stored = normalize_permissions_granted(
            getattr(member, 'permissions_granted', None)
        )
        if stored is not None:
            permissions = frozenset(stored)
        else:
            permissions = _load_system_permissions(
                user.org_id,
                membership_role,
                db=db,
                auth_user_id=user.user_id,
            )
        _store_membership(user.user_id, user.org_id, membership_role, permissions)
        return _apply_membership_role(
            user,
            membership_role,
            system_permissions=permissions,
        )
    except Exception:
        return user
    finally:
        db.close()


def _load_system_permissions(
    auth_org_id: str,
    role: str,
    *,
    db: Any | None = None,
    auth_user_id: str | None = None,
) -> frozenset[str]:
    """Resolve System permissions: member overrides, then role defaults/overrides."""
    try:
        if auth_user_id is not None:
            from app.services.member_permissions_service import resolve_member_permissions

            member_override = resolve_member_permissions(
                auth_org_id, auth_user_id, db=db
            )
            if member_override is not None:
                return member_override

        from app.services.role_permissions_service import resolve_role_permissions

        return resolve_role_permissions(auth_org_id, role, db=db)
    except Exception:
        from app.core.permissions import default_permissions_for_role
        from app.services.internal_org_service import is_reserved_internal_auth_org_id

        if is_reserved_internal_auth_org_id(auth_org_id):
            return frozenset(SYSTEM_PERMISSION_KEYS)
        return default_permissions_for_role(role)

def first_claim_value(claims: dict[str, Any], paths: list[str]) -> Any:
    for path in paths:
        value = claim_value(claims, path)
        if value is not None:
            return value
    return None


def claim_value(claims: dict[str, Any], path: str) -> Any:
    current: Any = claims
    for part in path.split('.'):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def development_user(
    authorization: str | None,
    x_org_id: str | None,
    x_user_id: str | None,
    x_user_role: str | None,
) -> CurrentUser:
    if authorization is not None and not authorization.lower().startswith('bearer '):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid Authorization header format',
        )

    return CurrentUser(
        user_id=x_user_id or 'dev-user',
        org_id=x_org_id or 'dev-org',
        role=normalize_role(x_user_role or MASTER_USER),
        roles=frozenset({normalize_role(x_user_role or MASTER_USER)}),
        claims={'auth_bypass': True},
        aal='aal2',
    )


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    x_org_id: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    x_user_role: str | None = Header(default=None),
) -> CurrentUser:
    settings = get_settings()
    if should_use_local_header_override(settings, x_org_id):
        user = development_user(authorization, x_org_id, x_user_id, x_user_role)
    else:
        state_user = getattr(request.state, 'current_user', None)
        if isinstance(state_user, CurrentUser):
            user = state_user
        elif should_use_auth_bypass(settings, authorization):
            user = development_user(authorization, x_org_id, x_user_id, x_user_role)
        else:
            user = get_jwt_verifier().verify_bearer_header(authorization)

    from app.db.session import set_tenant_org_id

    set_tenant_org_id(user.org_id)
    return enrich_user_from_membership(user)

def should_use_auth_bypass(settings: Settings, authorization: str | None) -> bool:
    """Allow local header-based auth when no bearer token is supplied."""
    return settings.auth_bypass_enabled and not settings.is_production and authorization is None


def should_use_local_header_override(settings: Settings, x_org_id: str | None) -> bool:
    """Allow local dev tools to target a seeded organization explicitly."""
    return settings.auth_bypass_enabled and not settings.is_production and bool(x_org_id)


def require_internal_role(*allowed_roles: str):
    normalized_roles = {normalize_role(role) for role in allowed_roles}

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_any_role(normalized_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail='Insufficient internal role')
        return user

    return dependency


def require_aal2(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Require MFA (Authenticator Assurance Level 2) for sensitive actions."""
    settings = get_settings()
    if settings.auth_bypass_enabled and not settings.is_production:
        return user
    mfa_enforced = (
        settings.mfa_required_for_sensitive_actions
        or settings.mfa_required_for_all_users
    )
    if mfa_enforced and not user.has_aal2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Multi-factor authentication is required for this action',
        )
    return user


def require_permission(*permissions: str):
    """Require at least one listed System permission (Clerk-parity catalog)."""
    requested = {permission.strip() for permission in permissions if permission.strip()}
    unknown = requested - SYSTEM_PERMISSION_KEYS
    if unknown:
        raise ValueError(f'Unknown system permission(s): {sorted(unknown)}')

    async def dependency(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_any_permission(*requested):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='Insufficient permission',
            )
        return user

    return dependency


@lru_cache
def get_jwt_verifier() -> SupabaseJWTVerifier:
    return SupabaseJWTVerifier(get_settings())


def get_clerk_jwt_verifier() -> SupabaseJWTVerifier:
    """Deprecated alias for get_jwt_verifier."""
    return get_jwt_verifier()
