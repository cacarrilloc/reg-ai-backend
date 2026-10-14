"""User account profile helpers (unique username <-> email)."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx
import structlog
from fastapi import HTTPException, status
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import UserProfile

logger = structlog.get_logger(__name__)

USERNAME_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]{1,30}[a-z0-9]$')


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_username(username: str) -> str:
    return str(username or '').strip().lower()


def validate_username(username: str) -> str:
    normalized = normalize_username(username)
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                'Username must be 3–32 characters, start and end with a letter or number, '
                'and use only lowercase letters, numbers, underscores, or hyphens.'
            ),
        )
    return normalized


def normalize_email(email: str) -> str:
    return str(email or '').strip().lower()


def is_username_available(db: Session, username: str) -> bool:
    normalized = validate_username(username)
    existing = (
        db.query(UserProfile.auth_user_id)
        .filter(UserProfile.username == normalized)
        .one_or_none()
    )
    return existing is None


def get_profile_for_user(db: Session, auth_user_id: str) -> UserProfile | None:
    auth_user_id = str(auth_user_id or '').strip()
    if not auth_user_id:
        return None
    return (
        db.query(UserProfile)
        .filter(UserProfile.auth_user_id == auth_user_id)
        .one_or_none()
    )


def get_email_for_username(db: Session, username: str) -> str | None:
    """Return the email bound to a username, or None when unknown."""
    normalized = validate_username(username)
    profile = (
        db.query(UserProfile)
        .filter(UserProfile.username == normalized)
        .one_or_none()
    )
    if profile is None:
        return None
    return normalize_email(profile.email)


def resolve_login_email(db: Session, identifier: str) -> str:
    """Resolve a username or email identifier to the login email address."""
    raw = str(identifier or '').strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Username or email is required.',
        )
    if '@' in raw:
        email = normalize_email(raw)
        if not email or '@' not in email or email.startswith('@') or email.endswith('@'):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Enter a valid email address.',
            )
        return email

    profile_email = get_email_for_username(db, raw)
    if not profile_email:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Invalid login credentials',
        )
    return profile_email


def claim_user_profile(
    db: Session,
    *,
    auth_user_id: str,
    username: str,
    email: str,
    settings: Settings | None = None,
) -> UserProfile:
    """Bind username + email to the authenticated Supabase user (idempotent)."""
    settings = settings or get_settings()
    auth_user_id = str(auth_user_id or '').strip()
    if not auth_user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Authenticated user id is required',
        )

    normalized_username = validate_username(username)
    normalized_email = normalize_email(email)
    if not normalized_email or '@' not in normalized_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='A valid email is required',
        )

    existing = get_profile_for_user(db, auth_user_id)
    if existing is not None:
        if existing.username != normalized_username:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='This account already has a different username.',
            )
        if normalize_email(existing.email) != normalized_email:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='This account is already linked to a different email.',
            )
        return existing

    username_taken = (
        db.query(UserProfile.auth_user_id)
        .filter(UserProfile.username == normalized_username)
        .one_or_none()
    )
    if username_taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='That username is already taken.',
        )

    email_taken = (
        db.query(UserProfile.auth_user_id)
        .filter(func.lower(UserProfile.email) == normalized_email)
        .one_or_none()
    )
    if email_taken is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='That email is already linked to another username.',
        )

    profile = UserProfile(
        auth_user_id=auth_user_id,
        username=normalized_username,
        email=normalized_email,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(profile)
    try:
        db.commit()
        db.refresh(profile)
    except IntegrityError as exc:
        db.rollback()
        logger.info(
            'user_profile_claim_conflict',
            auth_user_id=auth_user_id,
            username=normalized_username,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Username or email is already taken.',
        ) from exc
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception('user_profile_claim_failed', auth_user_id=auth_user_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to save user profile',
        ) from exc

    _sync_username_metadata(
        auth_user_id=auth_user_id,
        username=normalized_username,
        settings=settings,
    )
    logger.info(
        'user_profile_claimed',
        auth_user_id=auth_user_id,
        username=normalized_username,
    )
    return profile


def _sync_username_metadata(
    *,
    auth_user_id: str,
    username: str,
    settings: Settings,
) -> None:
    """Best-effort write of username into Supabase Auth user_metadata."""
    base = (settings.supabase_url or '').rstrip('/')
    key = (settings.supabase_service_role_key or '').strip()
    if not base or not key:
        return
    try:
        response = httpx.put(
            f'{base}/auth/v1/admin/users/{auth_user_id}',
            headers={
                'apikey': key,
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json={'user_metadata': {'username': username}},
            timeout=15.0,
        )
        if response.status_code >= 400:
            logger.warning(
                'user_metadata_username_sync_failed',
                status_code=response.status_code,
                body=response.text[:300],
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            'user_metadata_username_sync_error',
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
