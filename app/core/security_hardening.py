"""HTTP security controls: headers, IP allowlisting, and inbound rate limits."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from ipaddress import ip_address, ip_network
from threading import Lock

import structlog
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)

# Paths that remain reachable even when IP allowlisting is enabled
# (load balancers / platform health probes).
ALLOWLIST_EXEMPT_PATHS = frozenset(
    {
        '/',
        '/health',
        '/favicon.ico',
        '/api/v1/health',
    }
)

# Note: do NOT set Cross-Origin-Resource-Policy on API responses. CORP
# same-site/same-origin causes browsers to surface a NetworkError for
# legitimate cross-origin fetches from the Vercel/Next frontend
# (e.g. localhost:3000 → localhost:8000, or vercel.app → railway.app).
# COOP similarly applies to browsing contexts, not JSON APIs.
SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'X-XSS-Protection': '0',
}


def client_ip_from_request(request: Request) -> str:
    """Best-effort client IP behind Railway / Vercel / other reverse proxies."""
    forwarded = request.headers.get('x-forwarded-for') or request.headers.get('x-real-ip')
    if forwarded:
        return forwarded.split(',')[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return 'unknown'


def ip_is_allowed(client_ip: str, settings: Settings) -> bool:
    """Return whether ``client_ip`` is permitted by ALLOWED_IPS / ALLOWED_IP_CIDRS."""
    if not settings.ip_allowlist_enabled:
        return True
    if client_ip in {'unknown', 'testclient'}:
        # ASGI test client and unknown peers are blocked when allowlisting is on.
        return client_ip == 'testclient' and not settings.is_production

    try:
        address = ip_address(client_ip)
    except ValueError:
        return False

    if client_ip in settings.allowed_ip_list:
        return True

    for cidr in settings.allowed_ip_cidr_list:
        try:
            if address in ip_network(cidr, strict=False):
                return True
        except ValueError:
            logger.warning('invalid_allowed_ip_cidr', cidr=cidr)
    return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach baseline browser security headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        settings = get_settings()
        if not settings.security_headers_enabled:
            return response

        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)

        # HSTS only on HTTPS / production-like hosts to avoid breaking local http://.
        if settings.is_production or settings.is_staging:
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            )

        # Keep Swagger/ReDoc usable; apply a tight CSP everywhere else.
        path = request.url.path
        if path not in {'/docs', '/redoc', '/openapi.json'} and not path.startswith('/docs'):
            response.headers.setdefault(
                'Content-Security-Policy',
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
            )
        return response


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Optionally restrict API access to configured IPs / CIDRs (test/staging)."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        if not settings.ip_allowlist_enabled:
            return await call_next(request)

        path = request.url.path
        if path in ALLOWLIST_EXEMPT_PATHS or path.startswith('/api/v1/inngest'):
            return await call_next(request)

        client_ip = client_ip_from_request(request)
        if ip_is_allowed(client_ip, settings):
            return await call_next(request)

        logger.warning(
            'ip_allowlist_blocked',
            client_ip=client_ip,
            path=path,
            environment=settings.environment,
        )
        return JSONResponse(
            {'detail': 'Access from this network is not allowed'},
            status_code=status.HTTP_403_FORBIDDEN,
        )


class InboundRateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP sliding-window rate limit for inbound HTTP requests."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._auth_failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def _prune(self, bucket: deque[float], window_seconds: float, now: float) -> None:
        while bucket and now - bucket[0] > window_seconds:
            bucket.popleft()

    def _too_many(
        self,
        store: dict[str, deque[float]],
        key: str,
        limit: int,
        window_seconds: float = 60.0,
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = store[key]
            self._prune(bucket, window_seconds, now)
            if len(bucket) >= limit:
                return True
            bucket.append(now)
            return False

    def record_auth_failure(self, client_ip: str) -> bool:
        """Record an auth failure; return True when the IP should be throttled harder."""
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return False
        return self._too_many(
            self._auth_failures,
            client_ip,
            max(1, settings.rate_limit_auth_failures_per_minute),
        )

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        path = request.url.path
        if (
            not settings.rate_limit_enabled
            or path in ALLOWLIST_EXEMPT_PATHS
            or path.startswith('/api/v1/inngest')
        ):
            return await call_next(request)

        client_ip = client_ip_from_request(request)
        if self._too_many(
            self._hits,
            client_ip,
            max(1, settings.rate_limit_requests_per_minute),
        ):
            logger.warning('rate_limit_exceeded', client_ip=client_ip, path=path)
            return JSONResponse(
                {'detail': 'Rate limit exceeded. Try again shortly.'},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={'Retry-After': '60'},
            )

        response = await call_next(request)
        if response.status_code in {401, 403}:
            if self.record_auth_failure(client_ip):
                logger.warning(
                    'auth_failure_rate_limit_exceeded',
                    client_ip=client_ip,
                    path=path,
                )
        return response


def report_failed_authentication(
    *,
    reason: str,
    path: str,
    client_ip: str,
    detail: str | None = None,
) -> None:
    """Emit structured logs + Sentry breadcrumb/event for failed API authentication."""
    logger.warning(
        'authentication_failed',
        reason=reason,
        path=path,
        client_ip=client_ip,
        detail=(detail or '')[:200],
    )
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            scope.set_tag('security.event', 'authentication_failed')
            scope.set_tag('auth.failure_reason', reason)
            scope.set_extra('path', path)
            scope.set_extra('client_ip', client_ip)
            scope.fingerprint = ['authentication_failed', reason]
            sentry_sdk.capture_message(
                f'Authentication failed: {reason}',
                level='warning',
            )
    except Exception:
        # Observability must never break the auth path.
        return
