"""Auth/account endpoints: username availability and profile claim."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_platform_db
from app.services.mfa_admin_service import MfaAdminError, reset_user_mfa_factors
from app.services.user_profile_service import (
    claim_user_profile,
    get_profile_for_user,
    is_username_available,
    normalize_email,
    resolve_login_email,
    validate_username,
)

router = APIRouter(prefix='/auth', tags=['auth'])


class UsernameAvailableResponse(BaseModel):
    username: str
    available: bool


class LoginIdentifierRequest(BaseModel):
    identifier: str = Field(..., min_length=1, max_length=254)


class LoginIdentifierResponse(BaseModel):
    email: str


class UserProfileClaimRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: str | None = Field(default=None, max_length=254)


class UserProfileResponse(BaseModel):
    auth_user_id: str
    username: str
    email: str
    persisted: bool = True


@router.get('/usernames/{username}/available', response_model=UsernameAvailableResponse)
def get_username_available(
    username: str,
    db: Session | None = Depends(get_platform_db),
) -> UsernameAvailableResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    normalized = validate_username(username)
    return UsernameAvailableResponse(
        username=normalized,
        available=is_username_available(db, normalized),
    )


@router.post('/login-identifier', response_model=LoginIdentifierResponse)
def post_login_identifier(
    payload: LoginIdentifierRequest,
    db: Session | None = Depends(get_platform_db),
) -> LoginIdentifierResponse:
    """Resolve a username or email to the Supabase Auth email used for sign-in."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    return LoginIdentifierResponse(email=resolve_login_email(db, payload.identifier))


@router.get('/me/profile', response_model=UserProfileResponse)
def get_my_profile(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> UserProfileResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    profile = get_profile_for_user(db, user.user_id)
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='User profile not found',
        )
    return UserProfileResponse(
        auth_user_id=profile.auth_user_id,
        username=profile.username,
        email=profile.email,
    )


@router.post('/me/profile', response_model=UserProfileResponse, status_code=status.HTTP_201_CREATED)
def post_claim_my_profile(
    payload: UserProfileClaimRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> UserProfileResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )

    email = normalize_email(str(payload.email or ''))
    if not email:
        claims = user.claims if isinstance(user.claims, dict) else {}
        email = normalize_email(str(claims.get('email') or ''))
        user_metadata = claims.get('user_metadata')
        if not email and isinstance(user_metadata, dict):
            email = normalize_email(str(user_metadata.get('email') or ''))

    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Email is required to claim a username.',
        )

    profile = claim_user_profile(
        db,
        auth_user_id=user.user_id,
        username=payload.username,
        email=email,
    )
    return UserProfileResponse(
        auth_user_id=profile.auth_user_id,
        username=profile.username,
        email=profile.email,
    )


class MfaResetResponse(BaseModel):
    deleted_factors: int
    message: str


@router.post('/me/mfa/reset', response_model=MfaResetResponse)
def post_reset_my_mfa(
    user: CurrentUser = Depends(get_current_user),
) -> MfaResetResponse:
    """Clear MFA factors for the signed-in user so they can scan a new QR.

    Used when the stored factor does not match the authenticator entry for this
    environment (e.g. prod TOTP vs local/dev Auth). Requires a valid session
    (password/SSO); does not require AAL2.
    """
    settings = get_settings()
    try:
        deleted = reset_user_mfa_factors(settings=settings, user_id=user.user_id)
    except MfaAdminError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return MfaResetResponse(
        deleted_factors=deleted,
        message='Authenticator factors cleared. Set up a new authenticator app.',
    )
