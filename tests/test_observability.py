"""Tests for observability helpers."""


import pytest

from app.core.config import Settings
from app.core.observability import (
    configure_observability,
    is_valid_sentry_dsn,
    langfuse_configured,
    langfuse_workflow_trace,
    normalize_trace_tags,
    record_embedding_batch,
    record_langfuse_generation,
    resend_configured,
    reset_langfuse_client,
    sentry_configured,
    stringify_metadata,
    trace_event,
)


def test_sentry_configured_when_dsn_present() -> None:
    settings = Settings(SENTRY_DSN='https://example@sentry.io/1')
    assert bool(settings.sentry_dsn) is True
    assert sentry_configured() is False


def test_is_valid_sentry_dsn_rejects_placeholders() -> None:
    assert is_valid_sentry_dsn('https://...@....ingest.us.sentry.io/...') is False
    assert is_valid_sentry_dsn('https://example@sentry.io/1') is True
    assert is_valid_sentry_dsn('') is False


def test_configure_observability_skips_invalid_sentry_dsn(monkeypatch) -> None:
    import app.core.observability as observability

    observability._sentry_initialized = False
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(
            SENTRY_DSN='https://...@....ingest.us.sentry.io/...',
            LANGFUSE_PUBLIC_KEY='',
            LANGFUSE_SECRET_KEY='',
        ),
    )
    configure_observability()
    assert sentry_configured() is False


def test_langfuse_and_resend_config_flags(monkeypatch) -> None:
    reset_langfuse_client()
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(
            LANGFUSE_PUBLIC_KEY='',
            LANGFUSE_SECRET_KEY='',
            RESEND_API_KEY='',
        ),
    )
    assert langfuse_configured() is False
    assert resend_configured() is False

    langfuse_settings = Settings(
        LANGFUSE_PUBLIC_KEY='pk-lf-test',
        LANGFUSE_SECRET_KEY='sk-lf-test',
    )
    assert bool(
        langfuse_settings.langfuse_public_key and langfuse_settings.langfuse_secret_key
    )

    resend_settings = Settings(RESEND_API_KEY='re_test')
    assert bool(resend_settings.resend_api_key)


def test_stringify_metadata_truncates_values() -> None:
    metadata = stringify_metadata(
        {
            'run_id': 'abc-123',
            'long_value': 'x' * 500,
            'empty': '',
            'none_value': None,
        }
    )
    assert metadata['run_id'] == 'abc-123'
    assert len(metadata['long_value']) == 200
    assert 'empty' not in metadata
    assert 'none_value' not in metadata


def test_normalize_trace_tags() -> None:
    assert normalize_trace_tags('Gap Analysis', 'policy_update') == [
        'gap-analysis',
        'policy_update',
    ]


def test_trace_event_noops_without_langfuse_keys(monkeypatch) -> None:
    reset_langfuse_client()
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(LANGFUSE_PUBLIC_KEY='', LANGFUSE_SECRET_KEY=''),
    )
    trace_event('test.event', {'ok': True})


def test_record_langfuse_generation_noops_without_langfuse_keys(monkeypatch) -> None:
    reset_langfuse_client()
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(LANGFUSE_PUBLIC_KEY='', LANGFUSE_SECRET_KEY=''),
    )
    record_langfuse_generation('test.generation', model='claude-test', input_data={}, output_data='ok')


def test_record_embedding_batch_noops_without_langfuse_keys(monkeypatch) -> None:
    reset_langfuse_client()
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(LANGFUSE_PUBLIC_KEY='', LANGFUSE_SECRET_KEY=''),
    )
    record_embedding_batch(
        name='policy-chunk-embeddings',
        model='text-embedding-3-small',
        chunk_count=3,
        usage_tokens=42,
        preview='sample chunk',
    )


@pytest.mark.asyncio
async def test_langfuse_workflow_trace_noops_without_keys(monkeypatch) -> None:
    reset_langfuse_client()
    monkeypatch.setattr(
        'app.core.observability.get_settings',
        lambda: Settings(LANGFUSE_PUBLIC_KEY='', LANGFUSE_SECRET_KEY=''),
    )
    async with langfuse_workflow_trace('gap-analysis', user_id='org_test') as trace:
        assert trace is None
