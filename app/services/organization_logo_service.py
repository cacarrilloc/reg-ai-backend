"""Organization logo upload — persisted on ``organizations.logo_url``.

Logos are stored as ``data:<mime>;base64,...`` values directly on the
organization row so they remain readable without a separate storage fan-out.
Legacy ``storage://`` URIs are still resolved when present.
"""

from __future__ import annotations

import base64
import mimetypes
from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.models.core import Organization
from app.services.storage_service import (
    STORAGE_URI_PREFIX,
    download_file,
    parse_storage_uri,
)

logger = structlog.get_logger(__name__)

_ALLOWED_CONTENT_TYPES = {
    'image/png',
    'image/jpeg',
    'image/jpg',
    'image/webp',
    'image/gif',
    'image/svg+xml',
}
# Kept modest so ``/organizations/mine`` JSON stays lightweight.
_MAX_LOGO_BYTES = 512 * 1024


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _content_type_from_path(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed in _ALLOWED_CONTENT_TYPES:
        return guessed
    return 'image/png'


def public_or_signed_logo_url(logo_url: str | None, *, expires_in: int = 3600) -> str | None:
    """Return a browser-usable logo URL (data: or https)."""
    del expires_in
    if not logo_url:
        return None
    value = logo_url.strip()
    if not value:
        return None
    if value.startswith('data:') or value.startswith('http://') or value.startswith('https://'):
        return value
    if not value.startswith(STORAGE_URI_PREFIX):
        return None

    # Legacy Storage-backed logos: inline as a data URL so <img> works without
    # relying on a public bucket.
    try:
        stored = parse_storage_uri(value)
        raw = download_file(value)
        content_type = _content_type_from_path(stored.path)
        encoded = base64.b64encode(raw).decode('ascii')
        return f'data:{content_type};base64,{encoded}'
    except Exception as exc:
        logger.warning(
            'organization_logo_legacy_inline_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return None


async def upload_organization_logo(
    file: UploadFile,
    user: CurrentUser,
    db: Session | None,
) -> dict[str, str | None]:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    org_id = (user.org_id or '').strip()
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Active organization required')

    content_type = (file.content_type or '').split(';')[0].strip().lower()
    if content_type == 'image/jpg':
        content_type = 'image/jpeg'
    if content_type not in _ALLOWED_CONTENT_TYPES:
        # Some browsers omit content-type; infer from filename.
        guessed, _ = mimetypes.guess_type(file.filename or '')
        content_type = (guessed or '').lower()
        if content_type == 'image/jpg':
            content_type = 'image/jpeg'
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Logo must be a PNG, JPEG, WebP, GIF, or SVG image',
        )

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Logo file is empty')
    if len(contents) > _MAX_LOGO_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Logo must be 512KB or smaller',
        )

    data_url = f'data:{content_type};base64,{base64.b64encode(contents).decode("ascii")}'

    from app.services.organization_service import ensure_organization_row

    organization, _ = ensure_organization_row(
        db,
        auth_org_id=org_id,
        auth_user_id=user.user_id or None,
        role=user.role,
    )
    organization.logo_url = data_url
    organization.updated_at = _now()
    db.commit()
    db.refresh(organization)

    return {
        'logo_url': organization.logo_url,
        'logo_display_url': organization.logo_url,
    }


def delete_organization_logo(user: CurrentUser, db: Session | None) -> dict[str, None]:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )
    org_id = (user.org_id or '').strip()
    if not org_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Active organization required')

    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == org_id)
        .one_or_none()
    )
    if organization is None or not organization.logo_url:
        return {'logo_url': None, 'logo_display_url': None}

    organization.logo_url = None
    organization.updated_at = _now()
    db.commit()
    return {'logo_url': None, 'logo_display_url': None}


def migrate_legacy_storage_logo_if_needed(db: Session, organization: Organization) -> None:
    """One-time rewrite of storage:// logos into data: URLs on the org row."""
    raw = (organization.logo_url or '').strip()
    if not raw.startswith(STORAGE_URI_PREFIX):
        return
    inlined = public_or_signed_logo_url(raw)
    if not inlined or not inlined.startswith('data:'):
        return
    organization.logo_url = inlined
    organization.updated_at = _now()
    db.commit()
    db.refresh(organization)
