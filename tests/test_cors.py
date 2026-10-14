"""Tests for CORS behavior and auth middleware header propagation."""

from fastapi import HTTPException, status
from fastapi.testclient import TestClient

import app.core.security as security
from app.main import app


def test_localhost_preflight_allows_authorization_header() -> None:
    """Ensure localhost preflight requests allow the Authorization header."""
    client = TestClient(app)
    response = client.options(
        '/api/v1/policies/upload',
        headers={
            'Origin': 'http://127.0.0.1:3000',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type',
        },
    )

    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'http://127.0.0.1:3000'
    assert 'authorization' in response.headers['access-control-allow-headers'].lower()


def test_lan_preflight_allows_authorization_header() -> None:
    """Ensure LAN preflight requests allow the Authorization header."""
    client = TestClient(app)
    response = client.options(
        '/api/v1/policies/upload',
        headers={
            'Origin': 'http://192.168.4.30:3000',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type,x-org-id',
        },
    )

    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'http://192.168.4.30:3000'
    assert 'authorization' in response.headers['access-control-allow-headers'].lower()


def test_localhost_preflight_allows_private_network_access() -> None:
    """Ensure localhost preflight requests allow private network access."""
    client = TestClient(app)
    response = client.options(
        '/api/v1/policies/upload',
        headers={
            'Origin': 'http://localhost:3000',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'authorization,content-type,x-org-id',
            'Access-Control-Request-Private-Network': 'true',
        },
    )

    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'
    assert response.headers['access-control-allow-private-network'] == 'true'


def test_cors_origin_list_includes_frontend_url() -> None:
    """Configured FRONTEND_URL should be allowed even when omitted from CORS_ORIGINS."""
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT='production',
        CORS_ORIGINS='http://localhost:3000',
        FRONTEND_URL='https://reg-ai-frontend.vercel.app',
    )

    assert settings.cors_origin_list == [
        'http://localhost:3000',
        'https://reg-ai-frontend.vercel.app',
    ]
    assert settings.cors_frontend_configured is True
    assert settings.cors_origin_regex == r"https://[\w-]+(\.[\w-]+)*\.vercel\.app"


def test_non_production_cors_regex_allows_localhost_and_vercel_previews() -> None:
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT='dev',
        CORS_ORIGINS='https://reg-ai-dev.vercel.app',
        FRONTEND_URL='https://reg-ai-dev.vercel.app',
    )

    regex = settings.cors_origin_regex or ''
    assert 'vercel' in regex
    assert 'localhost' in regex


def test_cors_origin_list_includes_clerk_authorized_parties() -> None:
    """CLERK_AUTHORIZED_PARTIES should also be treated as an allowed browser origin."""
    from app.core.config import Settings

    settings = Settings(
        ENVIRONMENT='production',
        CORS_ORIGINS='http://localhost:3000',
        FRONTEND_URL='http://localhost:3000',
        CLERK_AUTHORIZED_PARTIES='https://reg-ai-frontend.vercel.app',
    )

    assert 'https://reg-ai-frontend.vercel.app' in settings.cors_origin_list
    assert settings.cors_frontend_configured is True


def test_auth_middleware_errors_include_cors_headers(monkeypatch) -> None:
    """Ensure auth middleware errors still include CORS response headers."""

    class RejectingVerifier:  # pylint: disable=too-few-public-methods
        """Test verifier that always rejects bearer tokens."""
        # type: ignore[no-untyped-def]

        def verify_bearer_header(self, authorization: str | None):
            """Always reject bearer tokens for testing.
            Args:
                authorization: The Authorization header value (ignored).
            """
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid Supabase Auth token')

    def _get_rejecting_verifier():
        return RejectingVerifier()

    monkeypatch.setattr(security, 'get_jwt_verifier',
                        _get_rejecting_verifier)

    client = TestClient(app)
    response = client.get(
        '/api/v1/policies',
        headers={
            'Origin': 'http://localhost:3000',
            'Authorization': 'Bearer invalid-token',
        },
    )

    assert response.status_code == 401
    assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'
    assert response.json()['detail'] == 'Invalid Supabase Auth token'


def test_auth_middleware_allows_local_org_override_when_bearer_is_invalid(monkeypatch) -> None:
    """Local dev should still authenticate with x-org-id when Clerk bearer is stale."""

    class RejectingVerifier:  # pylint: disable=too-few-public-methods
        # type: ignore[no-untyped-def]
        def verify_bearer_header(self, authorization: str | None):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid Clerk token')

    monkeypatch.setattr(security, 'get_clerk_jwt_verifier', lambda: RejectingVerifier())

    client = TestClient(app)
    response = client.get(
        '/api/v1/reviews/pending',
        headers={
            'Authorization': 'Bearer invalid-token',
            'x-org-id': 'org_example',
        },
    )

    assert response.status_code != 401
