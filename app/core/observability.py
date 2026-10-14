from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import structlog

from app.core.config import Settings, get_settings

logger = structlog.get_logger(__name__)

LANGFUSE_METADATA_MAX_CHARS = 200
LANGFUSE_PREVIEW_MAX_CHARS = 2_000
SENTRY_DSN_PLACEHOLDER_MARKERS = ('...',)
DEFAULT_LANGFUSE_HOST = 'https://cloud.langfuse.com'

_sentry_initialized = False
_langfuse_client: Any | None = None
_langfuse_client_resolved = False


def _sync_langfuse_environment(settings: Settings) -> None:
    """Ensure Langfuse SDK reads credentials from the same source as app settings."""
    if settings.langfuse_public_key:
        os.environ['LANGFUSE_PUBLIC_KEY'] = settings.langfuse_public_key
    if settings.langfuse_secret_key:
        os.environ['LANGFUSE_SECRET_KEY'] = settings.langfuse_secret_key
    host = (settings.langfuse_host or DEFAULT_LANGFUSE_HOST).rstrip('/')
    os.environ['LANGFUSE_HOST'] = host
    # Newer Langfuse SDKs prefer LANGFUSE_BASE_URL; keep both in sync.
    os.environ['LANGFUSE_BASE_URL'] = host
    os.environ.setdefault('LANGFUSE_TRACING_ENVIRONMENT', settings.environment)


def stringify_metadata(metadata: dict[str, Any] | None) -> dict[str, str]:
    """Coerce trace metadata to Langfuse-compatible string values."""
    if not metadata:
        return {}
    normalized: dict[str, str] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        normalized[str(key)[:64]] = text[:LANGFUSE_METADATA_MAX_CHARS]
    return normalized


def normalize_trace_tags(*parts: str | None) -> list[str]:
    tags: list[str] = []
    for part in parts:
        if not part:
            continue
        normalized = re.sub(r'[^a-zA-Z0-9_-]', '-', str(part).strip().lower())
        if normalized:
            tags.append(normalized[:64])
    return tags


def sanitize_trace_preview(value: Any, *, max_chars: int = LANGFUSE_PREVIEW_MAX_CHARS) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text[:max_chars] if len(text) > max_chars else text
    if isinstance(value, dict):
        return {
            str(key): sanitize_trace_preview(item, max_chars=max_chars)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_trace_preview(item, max_chars=max_chars) for item in value[:20]]
    text = str(value).strip()
    return text[:max_chars] if len(text) > max_chars else text


def is_valid_sentry_dsn(dsn: str | None) -> bool:
    """Return True when SENTRY_DSN looks like a real Sentry project DSN."""
    if not dsn:
        return False
    candidate = dsn.strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if any(marker in lowered for marker in SENTRY_DSN_PLACEHOLDER_MARKERS):
        return False
    if '<' in candidate or '>' in candidate:
        return False
    try:
        from sentry_sdk.utils import Dsn

        Dsn(candidate)
        return True
    except Exception:
        return False


def configure_observability() -> None:
    global _sentry_initialized
    settings = get_settings()
    if settings.langfuse_public_key and settings.langfuse_secret_key:
        reset_langfuse_client()
        client = get_langfuse_client()
        if client is None:
            logger.warning(
                'langfuse_not_configured',
                hint=(
                    'Langfuse keys are set but auth failed. Traces will not be sent. '
                    'Confirm LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are a matching pair '
                    f'for {settings.langfuse_host or DEFAULT_LANGFUSE_HOST}.'
                ),
            )
        else:
            logger.info(
                'langfuse_configured',
                host=settings.langfuse_host or DEFAULT_LANGFUSE_HOST,
                environment=settings.environment,
            )

    if not settings.sentry_dsn:
        return

    if not is_valid_sentry_dsn(settings.sentry_dsn):
        logger.warning(
            'sentry_dsn_invalid',
            hint='Set SENTRY_DSN to a real project DSN from Sentry Settings, or leave it empty.',
        )
        return

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        return

    try:
        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.environment,
            integrations=[FastApiIntegration(), LoggingIntegration()],
            traces_sample_rate=settings.sentry_traces_sample_rate,
            profiles_sample_rate=settings.sentry_profiles_sample_rate,
            send_default_pii=settings.sentry_send_default_pii,
        )
    except Exception as exc:
        logger.warning(
            'sentry_init_failed',
            error_type=type(exc).__name__,
            hint='Verify SENTRY_DSN or leave it empty to disable Sentry.',
        )
        return

    _sentry_initialized = True


def reset_langfuse_client() -> None:
    """Clear the cached Langfuse client so the next call re-initializes."""
    global _langfuse_client, _langfuse_client_resolved
    _langfuse_client = None
    _langfuse_client_resolved = False


def get_langfuse_client() -> Any | None:
    """Return a verified Langfuse client, or None when tracing should be skipped."""
    global _langfuse_client, _langfuse_client_resolved
    if _langfuse_client_resolved:
        return _langfuse_client

    settings = get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        _langfuse_client = None
        _langfuse_client_resolved = True
        return None

    try:
        from langfuse import Langfuse
    except ImportError:
        _langfuse_client = None
        _langfuse_client_resolved = True
        return None

    host = (settings.langfuse_host or DEFAULT_LANGFUSE_HOST).rstrip('/')
    _sync_langfuse_environment(settings)
    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=host,
        base_url=host,
        environment=settings.environment,
        timeout=10,
    )
    try:
        if not client.auth_check():
            logger.warning(
                'langfuse_auth_failed',
                host=host,
                hint=(
                    'Verify LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are a matching pair '
                    'for this project region. US cloud uses https://us.cloud.langfuse.com; '
                    'EU uses https://cloud.langfuse.com. Or clear both keys to disable Langfuse.'
                ),
            )
            # Do not cache auth failures permanently — allow a later retry after env fixes.
            _langfuse_client = None
            _langfuse_client_resolved = False
            return None
    except Exception as exc:
        logger.warning(
            'langfuse_auth_failed',
            error_type=type(exc).__name__,
            host=host,
            hint=(
                'Verify LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are a matching pair '
                'for this project region. US cloud uses https://us.cloud.langfuse.com; '
                'EU uses https://cloud.langfuse.com. Or clear both keys to disable Langfuse.'
            ),
        )
        _langfuse_client = None
        _langfuse_client_resolved = False
        return None

    _langfuse_client = client
    _langfuse_client_resolved = True
    return client


def flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        logger.warning(
            'langfuse_flush_failed',
            error_type=type(exc).__name__,
            hint='Trace data may not have reached Langfuse Cloud.',
        )


@asynccontextmanager
async def langfuse_workflow_trace(
    name: str,
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    input_data: Any = None,
):
    """Open a root workflow trace with propagated org/session attributes."""
    client = get_langfuse_client()
    if client is None:
        yield None
        return

    try:
        from langfuse import propagate_attributes
    except ImportError:
        yield None
        return

    string_metadata = stringify_metadata(metadata)
    with propagate_attributes(
        trace_name=name,
        user_id=user_id,
        session_id=session_id,
        tags=tags or None,
        metadata=string_metadata or None,
    ):
        with client.start_as_current_observation(
            name=name,
            as_type='chain',
            input=sanitize_trace_preview(input_data),
            metadata=string_metadata or None,
        ) as observation:
            try:
                yield observation
            except Exception as exc:
                observation.update(
                    level='ERROR',
                    status_message=str(exc)[:500] or type(exc).__name__,
                )
                raise
            finally:
                flush_langfuse()


@contextmanager
def langfuse_span(
    name: str,
    *,
    as_type: str = 'span',
    input_data: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any | None]:
    """Create a nested workflow step under the active Langfuse trace."""
    client = get_langfuse_client()
    if client is None:
        yield None
        return

    string_metadata = stringify_metadata(metadata)
    try:
        with client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=sanitize_trace_preview(input_data),
            metadata=string_metadata or None,
        ) as observation:
            yield observation
    except Exception:
        yield None


def sentry_configured() -> bool:
    return _sentry_initialized


def langfuse_configured() -> bool:
    return get_langfuse_client() is not None


def resend_configured() -> bool:
    return bool(get_settings().resend_api_key)


def trace_event(name: str, metadata: dict[str, Any] | None = None) -> None:
    """Record a lightweight milestone span inside the active workflow trace."""
    client = get_langfuse_client()
    if client is None:
        return

    string_metadata = stringify_metadata(metadata)
    try:
        with client.start_as_current_observation(
            name=name,
            as_type='span',
            metadata=string_metadata or None,
        ) as observation:
            if string_metadata:
                observation.update(output=string_metadata)
    except Exception:
        return


def record_langfuse_generation(
    name: str,
    *,
    model: str,
    input_data: Any,
    output_data: Any,
    metadata: dict[str, Any] | None = None,
    usage_details: dict[str, int] | None = None,
) -> None:
    """Record an LLM generation nested under the active workflow trace."""
    client = get_langfuse_client()
    if client is None:
        return

    string_metadata = stringify_metadata(metadata)
    try:
        with client.start_as_current_observation(
            name=name,
            as_type='generation',
            model=model,
            input=sanitize_trace_preview(input_data),
            metadata=string_metadata or None,
            usage_details=usage_details,
        ) as observation:
            observation.update(output=sanitize_trace_preview(output_data))
    except Exception:
        return


def record_embedding_batch(
    *,
    name: str,
    model: str,
    chunk_count: int,
    usage_tokens: int | None = None,
    preview: str | None = None,
) -> None:
    """Record an embedding batch under the active workflow trace."""
    client = get_langfuse_client()
    if client is None:
        return

    usage_details = {'total': usage_tokens} if usage_tokens is not None else None
    try:
        with client.start_as_current_observation(
            name=name,
            as_type='embedding',
            model=model,
            input={
                'chunk_count': chunk_count,
                'preview': sanitize_trace_preview(preview or '', max_chars=500),
            },
            usage_details=usage_details,
        ) as observation:
            observation.update(output={'embedded_chunks': chunk_count})
    except Exception:
        return


def record_trace_score(
    name: str,
    *,
    value: float | int | str,
    comment: str | None = None,
) -> None:
    """Record a score on the active Langfuse trace (e.g. CCO review decision)."""
    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.score_current_trace(
            name=name,
            value=value,
            comment=comment,
        )
    except Exception:
        return


def shutdown_observability() -> None:
    client = get_langfuse_client()
    if client is None:
        return
    try:
        client.flush()
        client.shutdown()
    except Exception as exc:
        logger.warning(
            'langfuse_shutdown_failed',
            error_type=type(exc).__name__,
        )
    finally:
        reset_langfuse_client()
