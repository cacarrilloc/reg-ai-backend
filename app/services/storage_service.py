from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import HTTPException, status
from storage3.exceptions import StorageApiError

from app.core.config import Settings, get_settings
from supabase import Client, create_client

logger = structlog.get_logger(__name__)

STORAGE_URI_PREFIX = 'storage://'
_ensured_buckets: set[str] = set()


@dataclass(frozen=True)
class StoredFile:
    bucket: str
    path: str

    @property
    def uri(self) -> str:
        return f'{STORAGE_URI_PREFIX}{self.bucket}/{self.path}'


def storage_url(settings: Settings) -> str:
    if not settings.supabase_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Supabase Storage is not configured',
        )

    normalized = settings.supabase_url.rstrip('/')
    return re.sub(r'/rest/v1$', '', normalized)


@lru_cache
def get_storage_client() -> Client:
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SUPABASE_SERVICE_ROLE_KEY is required for document storage',
        )
    return create_client(storage_url(settings), settings.supabase_service_role_key)


def get_storage_client_for_credentials(
    *,
    supabase_url: str,
    supabase_service_role_key: str,
) -> Client:
    normalized = supabase_url.rstrip('/')
    normalized = re.sub(r'/rest/v1$', '', normalized)
    return create_client(normalized, supabase_service_role_key)


def resolve_storage_client(*, org_id: str | None = None) -> Client:
    """Prefer tenant Storage credentials when org routing is active."""
    from app.db.session import get_tenant_org_id, resolve_storage_credentials_for_org

    effective_org = org_id if org_id is not None else get_tenant_org_id()
    url, key = resolve_storage_credentials_for_org(effective_org)
    settings = get_settings()
    if (
        url
        and key
        and (
            url.rstrip('/') != (settings.supabase_url or '').rstrip('/')
            or key != settings.supabase_service_role_key
        )
    ):
        return get_storage_client_for_credentials(
            supabase_url=url,
            supabase_service_role_key=key,
        )
    return get_storage_client()


def storage_path(org_id: str, category: str, record_id: str, filename: str) -> str:
    safe_segments = [safe_storage_segment(
        segment) for segment in (org_id, category, record_id)]
    return '/'.join([*safe_segments, safe_storage_filename(filename)])


def existing_storage_bucket_ids(client: Client) -> set[str]:
    buckets = client.storage.list_buckets()
    return {bucket.id for bucket in buckets}


def ensure_storage_bucket(
    bucket: str,
    *,
    client: Client | None = None,
    cache_key: str | None = None,
) -> None:
    """Create a Supabase Storage bucket when it is configured but missing."""
    ensured_key = cache_key or bucket
    if ensured_key in _ensured_buckets and client is None:
        return

    active = client or resolve_storage_client()
    try:
        if bucket in existing_storage_bucket_ids(active):
            _ensured_buckets.add(ensured_key)
            return
    except Exception as exc:
        logger.warning(
            'storage_bucket_list_failed',
            bucket=bucket,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )

    try:
        active.storage.create_bucket(bucket, options={'public': False})
        _ensured_buckets.add(ensured_key)
        logger.info('storage_bucket_created', bucket=bucket)
    except StorageApiError as exc:
        if 'already exists' in exc.message.lower():
            _ensured_buckets.add(ensured_key)
            return
        logger.warning(
            'storage_bucket_create_failed',
            bucket=bucket,
            error=exc.message,
            code=exc.code,
        )
    except Exception as exc:
        logger.warning(
            'storage_bucket_create_failed',
            bucket=bucket,
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )


def ensure_configured_storage_buckets(
    settings: Settings | None = None,
    *,
    supabase_url: str | None = None,
    supabase_service_role_key: str | None = None,
) -> None:
    """Ensure every configured Supabase bucket exists at startup."""
    settings = settings or get_settings()
    url = supabase_url or settings.supabase_url
    key = supabase_service_role_key or settings.supabase_service_role_key
    if not url or not key:
        return
    client: Client | None = None
    cache_prefix = ''
    if supabase_url and supabase_service_role_key:
        client = get_storage_client_for_credentials(
            supabase_url=supabase_url,
            supabase_service_role_key=supabase_service_role_key,
        )
        cache_prefix = f'{supabase_url.rstrip("/")}:'
    for bucket in settings.supabase_storage_bucket_list:
        ensure_storage_bucket(
            bucket,
            client=client,
            cache_key=f'{cache_prefix}{bucket}' if cache_prefix else None,
        )


def storage_upload_error_detail(exc: Exception) -> str:
    if isinstance(exc, StorageApiError):
        return exc.message
    return str(exc)[:500]


def upload_file(bucket: str, path: str, contents: bytes, content_type: str | None) -> StoredFile:
    client = resolve_storage_client()
    ensure_storage_bucket(bucket, client=client)
    try:
        client.storage.from_(bucket).upload(
            path,
            contents,
            {
                'content-type': content_type or 'application/octet-stream',
                'upsert': 'true',
            },
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            'storage_upload_failed',
            bucket=bucket,
            path=path,
            error_type=type(exc).__name__,
            error=storage_upload_error_detail(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to store document in Supabase Storage',
        ) from exc
    return StoredFile(bucket=bucket, path=path)


def create_signed_upload_url(bucket: str, path: str) -> dict[str, str]:
    """Return a browser-upload URL so large files never pass through Vercel or Railway."""
    client = resolve_storage_client()
    ensure_storage_bucket(bucket, client=client)
    try:
        result = client.storage.from_(bucket).create_signed_upload_url(path)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            'storage_signed_upload_url_failed',
            bucket=bucket,
            path=path,
            error_type=type(exc).__name__,
            error=storage_upload_error_detail(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to create a direct upload URL',
        ) from exc
    signed_url = str(result.get('signed_url') or result.get('signedUrl') or '')
    token = str(result.get('token') or '')
    if not signed_url:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='Unable to create a direct upload URL',
        )
    return {'signed_url': signed_url, 'token': token, 'path': path, 'bucket': bucket}


def storage_signed_url_error_detail(exc: Exception) -> str:
    if isinstance(exc, StorageApiError):
        return exc.message
    return str(exc)[:500]


def is_storage_object_missing_error(exc: Exception) -> bool:
    if isinstance(exc, StorageApiError):
        message = (exc.message or '').lower()
        if exc.status in {404, '404'}:
            return True
        if exc.status in {400, '400'} and any(
            token in message
            for token in ('not found', 'does not exist', 'object not found', 'no such key')
        ):
            return True
    if isinstance(exc, HTTPException) and exc.status_code == status.HTTP_404_NOT_FOUND:
        return True
    return False


def download_file(file_url: str) -> bytes:
    stored_file = parse_storage_uri(file_url)
    client = resolve_storage_client()
    ensure_storage_bucket(stored_file.bucket, client=client)
    bucket = client.storage.from_(stored_file.bucket)
    errors: list[str] = []

    try:
        return bucket.download(stored_file.path)
    except Exception as exc:
        errors.append(storage_signed_url_error_detail(exc))
        logger.warning(
            'storage_direct_download_failed',
            bucket=stored_file.bucket,
            path=stored_file.path,
            error_type=type(exc).__name__,
            error=storage_signed_url_error_detail(exc),
        )
        if is_storage_object_missing_error(exc):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail='Document not found in Supabase Storage',
            ) from exc

    for download in (True, False):
        try:
            response = bucket.create_signed_url(
                stored_file.path,
                900,
                {'download': True} if download else {},
            )
            signed_url = response.get('signedURL') or response.get('signedUrl')
            if not signed_url:
                continue
            fetch = httpx.get(str(signed_url), follow_redirects=True, timeout=60.0)
            fetch.raise_for_status()
            return fetch.content
        except Exception as exc:
            errors.append(storage_signed_url_error_detail(exc))
            logger.warning(
                'storage_signed_download_failed',
                bucket=stored_file.bucket,
                path=stored_file.path,
                download=download,
                error_type=type(exc).__name__,
                error=storage_signed_url_error_detail(exc),
            )
            if is_storage_object_missing_error(exc):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail='Document not found in Supabase Storage',
                ) from exc

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            'Unable to download document from Supabase Storage'
            + (f': {"; ".join(errors)}' if errors else '')
        ),
    )


def remove_file(stored_file: StoredFile) -> None:
    try:
        get_storage_client().storage.from_(
            stored_file.bucket).remove([stored_file.path])
    except Exception:
        # The database error is more actionable than cleanup of an orphaned object.
        return


def create_download_url(file_url: str, expires_in: int = 900) -> str:
    normalized = file_url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme in ('http', 'https'):
        return normalized

    stored_file = parse_storage_uri(normalized)
    ensure_storage_bucket(stored_file.bucket)
    bucket = get_storage_client().storage.from_(stored_file.bucket)

    last_error: Exception | None = None
    for download in (True, False):
        try:
            response = bucket.create_signed_url(
                stored_file.path,
                expires_in,
                {'download': True} if download else {},
            )
            signed_url = response.get('signedURL') or response.get('signedUrl')
            if signed_url:
                return str(signed_url)
        except Exception as exc:
            last_error = exc
            logger.warning(
                'storage_signed_url_failed',
                bucket=stored_file.bucket,
                path=stored_file.path,
                download=download,
                error_type=type(exc).__name__,
                error=storage_signed_url_error_detail(exc),
            )

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            'Unable to create a document download URL'
            + (f': {storage_signed_url_error_detail(last_error)}' if last_error else '')
        ),
    )


def create_inline_url(file_url: str, expires_in: int = 3600) -> str:
    """Signed URL for displaying a stored image in the browser (not a download)."""
    normalized = file_url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme in ('http', 'https'):
        return normalized

    stored_file = parse_storage_uri(normalized)
    ensure_storage_bucket(stored_file.bucket)
    bucket = get_storage_client().storage.from_(stored_file.bucket)
    last_error: Exception | None = None
    try:
        response = bucket.create_signed_url(stored_file.path, expires_in)
        signed_url = response.get('signedURL') or response.get('signedUrl')
        if signed_url:
            return str(signed_url)
    except Exception as exc:
        last_error = exc
        logger.warning(
            'storage_inline_url_failed',
            bucket=stored_file.bucket,
            path=stored_file.path,
            error_type=type(exc).__name__,
            error=storage_signed_url_error_detail(exc),
        )
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            'Unable to create a preview URL'
            + (f': {storage_signed_url_error_detail(last_error)}' if last_error else '')
        ),
    )


def parse_storage_uri(file_url: str) -> StoredFile:
    parsed = urlparse(file_url.strip())
    if parsed.scheme not in {'storage', 'supabase'} or not parsed.netloc or not parsed.path.strip('/'):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail='Policy file is not stored in Supabase Storage',
        )
    return StoredFile(bucket=parsed.netloc, path=parsed.path.strip('/'))


def safe_storage_segment(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', '_', value).strip('._') or 'unknown'


def safe_storage_filename(value: str) -> str:
    return safe_storage_segment(value.replace('/', '_').replace('\\', '_'))[:180]
