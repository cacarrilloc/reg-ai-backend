"""Thin client for Supabase Auth Admin SSO (SAML) provider API."""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


class SupabaseSsoAdminError(Exception):
    """Raised when the Supabase SSO Admin API rejects a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _base_url(settings: Settings) -> str:
    url = (settings.supabase_url or '').rstrip('/')
    if not url:
        raise SupabaseSsoAdminError('SUPABASE_URL is not configured', status_code=503)
    return url


def _headers(settings: Settings) -> dict[str, str]:
    key = (settings.supabase_service_role_key or '').strip()
    if not key:
        raise SupabaseSsoAdminError(
            'SUPABASE_SERVICE_ROLE_KEY is not configured',
            status_code=503,
        )
    return {
        'apikey': key,
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
    }


def saml_sp_info(settings: Settings) -> dict[str, str]:
    """Service Provider URLs to paste into the customer IdP."""
    base = _base_url(settings)
    return {
        'entity_id': f'{base}/auth/v1/sso/saml/metadata',
        'acs_url': f'{base}/auth/v1/sso/saml/acs',
        'metadata_url': f'{base}/auth/v1/sso/saml/metadata',
    }


def create_saml_provider(
    settings: Settings,
    *,
    domains: list[str],
    metadata_url: str | None = None,
    metadata_xml: str | None = None,
    attribute_groups_key: str = 'groups',
) -> dict[str, Any]:
    """Register a SAML IdP with Supabase Auth Admin API."""
    if not metadata_url and not metadata_xml:
        raise SupabaseSsoAdminError('metadata_url or metadata_xml is required', status_code=400)

    body: dict[str, Any] = {
        'type': 'saml',
        'domains': domains,
        'attribute_mapping': {
            'keys': {
                'email': {
                    'name': 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress',
                },
                'name': {
                    'name': 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name',
                },
                attribute_groups_key: {
                    'name': 'http://schemas.xmlsoap.org/claims/Group',
                    'array': True,
                },
            }
        },
    }
    if metadata_url:
        body['metadata_url'] = metadata_url
    if metadata_xml:
        body['metadata_xml'] = metadata_xml

    url = f'{_base_url(settings)}/auth/v1/admin/sso/providers'
    try:
        response = httpx.post(url, headers=_headers(settings), json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SupabaseSsoAdminError(f'Could not reach Supabase Auth: {exc}', status_code=502) from exc

    if response.status_code >= 400:
        detail = (response.text or '')[:500]
        logger.warning(
            'supabase_sso_provider_create_failed',
            status=response.status_code,
            detail=detail,
        )
        raise SupabaseSsoAdminError(
            detail or f'Supabase rejected SSO provider create ({response.status_code})',
            status_code=response.status_code,
        )
    data = response.json()
    if not isinstance(data, dict) or not data.get('id'):
        raise SupabaseSsoAdminError('Supabase did not return a provider id', status_code=502)
    return data


def update_saml_provider(
    settings: Settings,
    provider_id: str,
    *,
    domains: list[str] | None = None,
    metadata_url: str | None = None,
    metadata_xml: str | None = None,
    attribute_groups_key: str = 'groups',
) -> dict[str, Any]:
    body: dict[str, Any] = {
        'attribute_mapping': {
            'keys': {
                'email': {
                    'name': 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress',
                },
                'name': {
                    'name': 'http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name',
                },
                attribute_groups_key: {
                    'name': 'http://schemas.xmlsoap.org/claims/Group',
                    'array': True,
                },
            }
        },
    }
    if domains is not None:
        body['domains'] = domains
    if metadata_url:
        body['metadata_url'] = metadata_url
    if metadata_xml:
        body['metadata_xml'] = metadata_xml

    url = f'{_base_url(settings)}/auth/v1/admin/sso/providers/{provider_id}'
    try:
        response = httpx.put(url, headers=_headers(settings), json=body, timeout=30.0)
    except httpx.HTTPError as exc:
        raise SupabaseSsoAdminError(f'Could not reach Supabase Auth: {exc}', status_code=502) from exc

    if response.status_code >= 400:
        detail = (response.text or '')[:500]
        raise SupabaseSsoAdminError(
            detail or f'Supabase rejected SSO provider update ({response.status_code})',
            status_code=response.status_code,
        )
    data = response.json()
    if not isinstance(data, dict):
        raise SupabaseSsoAdminError('Invalid Supabase SSO update response', status_code=502)
    return data


def delete_saml_provider(settings: Settings, provider_id: str) -> None:
    url = f'{_base_url(settings)}/auth/v1/admin/sso/providers/{provider_id}'
    try:
        response = httpx.delete(url, headers=_headers(settings), timeout=30.0)
    except httpx.HTTPError as exc:
        raise SupabaseSsoAdminError(f'Could not reach Supabase Auth: {exc}', status_code=502) from exc

    if response.status_code in {404, 204}:
        return
    if response.status_code >= 400:
        detail = (response.text or '')[:500]
        raise SupabaseSsoAdminError(
            detail or f'Supabase rejected SSO provider delete ({response.status_code})',
            status_code=response.status_code,
        )
