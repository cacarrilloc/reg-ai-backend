"""Provider-neutral retry and user-message helpers for workflow retry loops."""

from __future__ import annotations

from app.ai.catalog import provider_label as catalog_provider_label
from app.ai.types import (
    AIAuthError,
    AIBillingError,
    AIConfigurationError,
    AICredentialMissing,
    AIEncryptionUnavailable,
    AIProviderError,
    AIRateLimitError,
)
from app.core.config import Settings


def is_rate_limit_error(exc: Exception | None) -> bool:
    return isinstance(exc, AIRateLimitError)


def is_billing_error(exc: Exception | None) -> bool:
    return isinstance(exc, AIBillingError)


def is_non_retryable_error(exc: Exception | None) -> bool:
    """Billing, auth, and configuration failures will not succeed on retry."""
    if isinstance(exc, AICredentialMissing | AIConfigurationError | AIEncryptionUnavailable):
        return True
    return isinstance(exc, AIProviderError) and not exc.retryable


def retry_delay(exc: Exception, attempt: int, settings: Settings) -> float:
    if is_non_retryable_error(exc):
        return 0.0
    if is_rate_limit_error(exc):
        return settings.anthropic_rate_limit_retry_seconds
    return settings.anthropic_retry_delay_seconds * attempt


def provider_label(exc: Exception | None) -> str:
    provider = getattr(exc, 'provider', '') or ''
    return catalog_provider_label(provider) if provider else 'AI provider'


def user_error_message(exc: Exception | None) -> str:
    if exc is None:
        return 'Unknown AI provider error.'
    provider = provider_label(exc)
    if is_billing_error(exc):
        return f'{provider} API credits are exhausted. Add credits with {provider}, then re-run analysis.'
    if isinstance(exc, AIAuthError):
        return f'{provider} authentication failed. Verify the API key in AI settings and retry.'
    detail = str(exc).strip()
    return detail[:237] + '...' if len(detail) > 240 else detail
