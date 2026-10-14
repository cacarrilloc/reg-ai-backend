from __future__ import annotations

import re
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

from app.core.observability import (
    flush_langfuse,
    get_langfuse_client,
    normalize_trace_tags,
    sanitize_trace_preview,
    stringify_metadata,
)
from app.core.security import PUBLIC_PATHS, CurrentUser

_SKIP_TRACE_PREFIXES = (
    '/docs',
    '/redoc',
    '/openapi.json',
    '/favicon.ico',
)
_SKIP_TRACE_PATHS = PUBLIC_PATHS | {'/api/v1/inngest'}


def should_trace_request(path: str) -> bool:
    if path in _SKIP_TRACE_PATHS:
        return False
    return not any(path.startswith(prefix) for prefix in _SKIP_TRACE_PREFIXES)


def api_feature_from_path(path: str) -> str:
    match = re.search(r'/api/v1/([^/?]+)', path)
    if not match:
        return 'api'
    return match.group(1).replace('_', '-')


def api_trace_name(method: str, path: str) -> str:
    feature = api_feature_from_path(path)
    return f'api.{method.lower()}.{feature}'


def request_trace_context(request: Request) -> dict[str, Any]:
    user = getattr(request.state, 'current_user', None)
    if isinstance(user, CurrentUser):
        return {
            'user_id': user.user_id or None,
            'session_id': user.org_id or None,
            'metadata': stringify_metadata(
                {
                    'org_id': user.org_id,
                    'role': user.role,
                }
            ),
        }

    org_id = request.headers.get('x-org-id')
    user_id = request.headers.get('x-user-id')
    if org_id or user_id:
        return {
            'user_id': user_id,
            'session_id': org_id,
            'metadata': stringify_metadata({'org_id': org_id or ''}),
        }
    return {}


class LangfuseTracingMiddleware(BaseHTTPMiddleware):
    """Attach a Langfuse root span to each authenticated API request."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if not should_trace_request(path):
            return await call_next(request)

        client = get_langfuse_client()
        if client is None:
            return await call_next(request)

        try:
            from langfuse import propagate_attributes
        except ImportError:
            return await call_next(request)

        context = request_trace_context(request)
        trace_name = api_trace_name(request.method, path)
        tags = normalize_trace_tags('api', api_feature_from_path(path), request.method.lower())

        with propagate_attributes(
            trace_name=trace_name,
            user_id=context.get('user_id'),
            session_id=context.get('session_id'),
            tags=tags,
            metadata=context.get('metadata') or None,
        ):
            try:
                with client.start_as_current_observation(
                    name=trace_name,
                    as_type='span',
                    input=sanitize_trace_preview(
                        {
                            'method': request.method,
                            'path': path,
                        }
                    ),
                    metadata=context.get('metadata') or None,
                ) as observation:
                    try:
                        response = await call_next(request)
                    except Exception as exc:
                        observation.update(
                            level='ERROR',
                            status_message=str(exc)[:500] or type(exc).__name__,
                        )
                        raise
                    observation.update(
                        output=sanitize_trace_preview({'status_code': response.status_code}),
                    )
                    return response
            finally:
                flush_langfuse()
