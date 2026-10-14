"""Tests for HTTP security headers, IP allowlisting, and inbound rate limits."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.security_hardening import (
    InboundRateLimitMiddleware,
    IPAllowlistMiddleware,
    SecurityHeadersMiddleware,
    ip_is_allowed,
)


def test_ip_is_allowed_exact_and_cidr() -> None:
    settings = Settings(
        ALLOWED_IPS='203.0.113.10',
        ALLOWED_IP_CIDRS='10.0.0.0/8',
    )
    assert ip_is_allowed('203.0.113.10', settings) is True
    assert ip_is_allowed('10.1.2.3', settings) is True
    assert ip_is_allowed('198.51.100.1', settings) is False


def test_ip_allowlist_disabled_when_empty() -> None:
    settings = Settings(ALLOWED_IPS='', ALLOWED_IP_CIDRS='')
    assert settings.ip_allowlist_enabled is False
    assert ip_is_allowed('198.51.100.1', settings) is True


def test_security_headers_middleware_sets_baseline_headers(monkeypatch) -> None:
    monkeypatch.setenv('ENVIRONMENT', 'staging')
    monkeypatch.setenv('SECURITY_HEADERS_ENABLED', 'true')
    from app.core import config as config_module

    config_module.get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get('/ping')
    def ping() -> dict[str, str]:
        return {'status': 'ok'}

    client = TestClient(app)
    response = client.get('/ping')
    assert response.status_code == 200
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['X-Frame-Options'] == 'DENY'
    assert 'Strict-Transport-Security' in response.headers
    assert 'Content-Security-Policy' in response.headers
    # CORP/COOP must stay off API responses or browsers block cross-origin fetch.
    assert 'cross-origin-resource-policy' not in {
        k.lower() for k in response.headers.keys()
    }
    assert 'cross-origin-opener-policy' not in {
        k.lower() for k in response.headers.keys()
    }
    config_module.get_settings.cache_clear()


def test_ip_allowlist_middleware_blocks_unknown_ip(monkeypatch) -> None:
    monkeypatch.setenv('ALLOWED_IPS', '203.0.113.10')
    monkeypatch.setenv('ALLOWED_IP_CIDRS', '')
    from app.core import config as config_module

    config_module.get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(IPAllowlistMiddleware)

    @app.get('/secure')
    def secure() -> dict[str, str]:
        return {'status': 'ok'}

    @app.get('/health')
    def health() -> dict[str, str]:
        return {'status': 'ok'}

    client = TestClient(app)
    blocked = client.get('/secure', headers={'X-Forwarded-For': '198.51.100.1'})
    assert blocked.status_code == 403
    allowed = client.get('/secure', headers={'X-Forwarded-For': '203.0.113.10'})
    assert allowed.status_code == 200
    allowed_health = client.get('/health', headers={'X-Forwarded-For': '198.51.100.1'})
    assert allowed_health.status_code == 200
    config_module.get_settings.cache_clear()


def test_inbound_rate_limit_middleware_returns_429(monkeypatch) -> None:
    monkeypatch.setenv('RATE_LIMIT_ENABLED', 'true')
    monkeypatch.setenv('RATE_LIMIT_REQUESTS_PER_MINUTE', '3')
    from app.core import config as config_module

    config_module.get_settings.cache_clear()

    app = FastAPI()
    app.add_middleware(InboundRateLimitMiddleware)

    @app.get('/busy')
    def busy() -> dict[str, str]:
        return {'status': 'ok'}

    client = TestClient(app)
    assert client.get('/busy').status_code == 200
    assert client.get('/busy').status_code == 200
    assert client.get('/busy').status_code == 200
    limited = client.get('/busy')
    assert limited.status_code == 429
    assert limited.headers.get('Retry-After') == '60'
    config_module.get_settings.cache_clear()
