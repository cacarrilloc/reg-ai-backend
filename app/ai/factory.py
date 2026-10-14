"""Entry points for workflows. Returns Reg-AI interfaces, never LiteLLM objects."""

from __future__ import annotations

from app.ai.credentials import CredentialResolver
from app.ai.litellm_adapter import (
    LiteLLMEmbeddingProvider,
    LiteLLMGenerationProvider,
    check_api_key,
    probe_dimensions,
)
from app.ai.types import (
    AIConfigurationError,
    AICredentialMissing,
    AIEncryptionUnavailable,
    ChatMessage,
    EmbeddingProvider,
    LLMProvider,
    Purpose,
    ResolvedModel,
)
from app.core.config import Settings

_UNAVAILABLE = (AICredentialMissing, AIConfigurationError, AIEncryptionUnavailable)


def llm_provider_for_org(
    org_id: str | None,
    purpose: Purpose = 'analysis',
    *,
    platform_model: str | None = None,
    resolver: CredentialResolver | None = None,
) -> LLMProvider:
    if purpose == 'embedding':
        raise ValueError('Use embedding_provider_for_org for embeddings')
    target = (resolver or CredentialResolver()).for_org(org_id, purpose, platform_model=platform_model)
    return LiteLLMGenerationProvider(target)


def optional_llm_provider(
    org_id: str | None,
    purpose: Purpose = 'analysis',
    *,
    platform_model: str | None = None,
) -> LLMProvider | None:
    """None when the org has no usable model/credentials, so workflows use their fallbacks."""
    try:
        return llm_provider_for_org(org_id, purpose, platform_model=platform_model)
    except _UNAVAILABLE:
        return None


def embedding_provider_for_org(
    org_id: str | None,
    *,
    resolver: CredentialResolver | None = None,
) -> EmbeddingProvider:
    return LiteLLMEmbeddingProvider((resolver or CredentialResolver()).for_org(org_id, 'embedding'))


def optional_embedding_provider(org_id: str | None) -> EmbeddingProvider | None:
    try:
        return embedding_provider_for_org(org_id)
    except _UNAVAILABLE:
        return None


def embedding_model_label(org_id: str | None, settings: Settings) -> str:
    """Model name recorded in chunk/analysis metadata."""
    provider = optional_embedding_provider(org_id)
    return provider.model_label if provider else settings.openai_embedding_model


async def probe_model(target: ResolvedModel, *, settings: Settings | None = None) -> int | None:
    """Make one tiny call. Returns the native embedding width, or None for generation."""
    if target.kind == 'embedding':
        return await probe_dimensions(target, settings)
    await LiteLLMGenerationProvider(target, settings=settings).generate(
        [ChatMessage('user', 'Reply with OK.')], max_tokens=5, trace_name='ai.probe')
    return None


async def check_credential(target: ResolvedModel, *, settings: Settings | None = None) -> int | None:
    """Confirm a key is accepted. Does not require the selected model to generate a reply."""
    return await check_api_key(target, settings)
