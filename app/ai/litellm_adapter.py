"""The only module allowed to import LiteLLM."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

import httpx

from app.ai.metering import record_ai_usage
from app.ai.throttle import throttle_platform_anthropic
from app.ai.types import (
    AIAuthError,
    AIBillingError,
    AIConfigurationError,
    AIProviderError,
    AIRateLimitError,
    ChatMessage,
    EmbeddingResult,
    GenerationResult,
    ImagePart,
    ModelMapEntry,
    ResolvedModel,
    TextPart,
    ThrottleTier,
    model_label,
)
from app.core.config import Settings, get_settings
from app.core.observability import record_embedding_batch, record_langfuse_generation
from app.services.rate_limiter import AsyncRateLimiter

EMBEDDING_BATCH_SIZE = 64
_BILLING_MARKERS = ('credit balance', 'insufficient credits', 'insufficient_quota', 'billing', 'payment required')
_AUTH_MARKERS = (
    'unauthorized', 'unauthenticated', 'forbidden', 'not authorized', 'authentication failed',
    'invalid api key', 'invalid_api_key', 'incorrect api key', 'invalid x-api-key',
    'permission denied', 'authenticationerror',
)
_AUTH_STATUS = re.compile(r'\b(401|403)\b')
_NON_RETRYABLE = {'BadRequestError', 'NotFoundError', 'ContextWindowExceededError',
                  'ContentPolicyViolationError', 'UnsupportedParamsError'}
# LiteLLM also reads these from the process env. A BYOK call must not fall back to Reg-AI keys.
_PLATFORM_ENV = (
    'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'AZURE_API_KEY', 'AZURE_OPENAI_API_KEY',
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'XAI_API_KEY', 'GROQ_API_KEY', 'MISTRAL_API_KEY',
    'TOGETHER_API_KEY', 'COHERE_API_KEY', 'DEEPSEEK_API_KEY', 'FIREWORKS_API_KEY',
    'OPENROUTER_API_KEY', 'HUGGINGFACE_API_KEY', 'PERPLEXITY_API_KEY', 'VOYAGE_API_KEY',
    'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_PROFILE', 'AWS_DEFAULT_PROFILE', 'AWS_REGION', 'AWS_DEFAULT_REGION',
    'AWS_BEARER_TOKEN_BEDROCK', 'AWS_WEB_IDENTITY_TOKEN_FILE',
    'AWS_ROLE_ARN', 'AWS_ROLE_SESSION_NAME', 'GOOGLE_APPLICATION_CREDENTIALS',
)
# Stop boto3 from using the host's instance role during a BYOK probe.
_BYOK_FORCE_ENV = {'AWS_EC2_METADATA_DISABLED': 'true'}
_PROVIDER_API_KEY_ENV = {
    'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY', 'azure': 'AZURE_API_KEY',
    'gemini': 'GEMINI_API_KEY', 'xai': 'XAI_API_KEY', 'groq': 'GROQ_API_KEY', 'mistral': 'MISTRAL_API_KEY',
    'together_ai': 'TOGETHER_API_KEY', 'cohere': 'COHERE_API_KEY', 'deepseek': 'DEEPSEEK_API_KEY',
    'fireworks_ai': 'FIREWORKS_API_KEY', 'openrouter': 'OPENROUTER_API_KEY',
    'huggingface': 'HUGGINGFACE_API_KEY', 'perplexity': 'PERPLEXITY_API_KEY', 'voyage': 'VOYAGE_API_KEY',
}
_LITELLM_KEY_ATTRS = ('api_key', 'openai_key', 'anthropic_key', 'azure_key')
_limiters: dict[tuple[str, str | None, str, str | None], AsyncRateLimiter | None] = {}
_byok_env_lock = asyncio.Lock()


@lru_cache
def _litellm() -> Any:
    # Never fetch the remote model-cost map or send LiteLLM telemetry.
    os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')
    import litellm

    litellm.telemetry = False
    litellm.suppress_debug_info = True
    # Mythos and other Anthropic models reject temperature=0. Drop those params
    # instead of failing gap analysis / business-change runs.
    litellm.drop_params = True
    litellm.num_retries = 0
    return litellm


def supported_provider_ids() -> frozenset[str]:
    return frozenset(str(getattr(p, 'value', p)) for p in _litellm().provider_list)


_MODEL_MAP_MODES = {'chat': 'generation', 'embedding': 'embedding'}


def model_map_entries() -> tuple[ModelMapEntry, ...]:
    """Chat and embedding models from LiteLLM's bundled (local, offline) model map."""
    entries: list[ModelMapEntry] = []
    for key, info in _litellm().model_cost.items():
        if not isinstance(info, Mapping):
            continue
        kind = _MODEL_MAP_MODES.get(str(info.get('mode') or ''))
        provider = str(info.get('litellm_provider') or '')
        if kind is None or not provider:
            continue
        model = key.removeprefix(f'{provider}/')
        dimensions = info.get('output_vector_size')
        entries.append(ModelMapEntry(
            provider=provider,
            model=model,
            kind=kind,  # type: ignore[arg-type]
            dimensions=int(dimensions) if isinstance(dimensions, int) and dimensions > 0 else None,
            vision=bool(info.get('supports_vision')),
            max_input_tokens=info.get('max_input_tokens') if isinstance(info.get('max_input_tokens'), int) else None,
        ))
    return tuple(entries)


def _limiter(target: ResolvedModel, settings: Settings) -> AsyncRateLimiter | None:
    """One limiter per paying credential/endpoint, so one org cannot starve another.

    Platform Anthropic calls are throttled per call site (``throttle=``) instead."""
    key = (target.source, target.org_id if target.source == 'byok' else None, target.provider, target.api_base)
    if key not in _limiters:
        if target.source == 'byok':
            rate: float | None = settings.ai_requests_per_second
        else:
            rate = settings.openai_requests_per_second if target.provider == 'openai' else None
        _limiters[key] = AsyncRateLimiter(max(rate, 0.1)) if rate else None
    return _limiters[key]


async def _wait(target: ResolvedModel, settings: Settings) -> None:
    limiter = _limiter(target, settings)
    if limiter is not None:
        await limiter.wait()


def _chars(system: str | None, messages: Sequence[ChatMessage]) -> int:
    total = len(system or '')
    for message in messages:
        if isinstance(message.content, str):
            total += len(message.content)
            continue
        for part in message.content:
            if isinstance(part, TextPart):
                total += len(part.text)
            else:
                total += min(max(len(part.data_base64) // 4, 2_000), 12_000)
    return total


def _redact(text: str, target: ResolvedModel) -> str:
    for value in target.auth.values():
        if value and len(value) >= 6:
            text = text.replace(value, '[redacted]')
    return text[:240]


def _is_auth_failure(name: str, detail: str) -> bool:
    if name in {'AuthenticationError', 'PermissionDeniedError'}:
        return True
    lower = detail.lower()
    return any(marker in lower for marker in _AUTH_MARKERS) or bool(_AUTH_STATUS.search(detail))


def _translate_error(exc: Exception, target: ResolvedModel) -> AIProviderError:
    detail = _redact(str(exc), target)
    name = type(exc).__name__
    provider = target.provider
    if any(marker in detail.lower() for marker in _BILLING_MARKERS):
        return AIBillingError(f'{provider} billing error: {detail}', provider=provider)
    if _is_auth_failure(name, detail):
        return AIAuthError(f'{provider} rejected the credentials.', provider=provider)
    if name == 'RateLimitError':
        return AIRateLimitError(f'{provider} rate limit reached.', provider=provider)
    error = AIProviderError(f'{provider} request failed: {detail}', provider=provider)
    error.retryable = name not in _NON_RETRYABLE
    return error


def _byok_runtime_env(target: ResolvedModel) -> dict[str, str]:
    """Put the org's key in the env LiteLLM/SDKs actually read, never the platform key."""
    env = dict(_BYOK_FORCE_ENV)
    auth = target.auth
    key = auth.get('api_key')
    env_name = _PROVIDER_API_KEY_ENV.get(target.provider)
    if env_name and key:
        env[env_name] = key
        if target.provider == 'gemini':
            env['GOOGLE_API_KEY'] = key
    for field, name in (
        ('aws_access_key_id', 'AWS_ACCESS_KEY_ID'),
        ('aws_secret_access_key', 'AWS_SECRET_ACCESS_KEY'),
        ('aws_session_token', 'AWS_SESSION_TOKEN'),
        ('aws_region_name', 'AWS_REGION'),
    ):
        value = auth.get(field)
        if value:
            env[name] = value
    return env


def _platform_runtime_env(target: ResolvedModel) -> dict[str, str]:
    """Pin the paying platform key so LiteLLM cannot read a stripped or BYOK env."""
    env: dict[str, str] = {}
    key = (target.auth or {}).get('api_key')
    env_name = _PROVIDER_API_KEY_ENV.get(target.provider)
    if env_name and key:
        env[env_name] = key
        if target.provider == 'gemini':
            env['GOOGLE_API_KEY'] = key
    return env


@asynccontextmanager
async def _without_platform_credentials(target: ResolvedModel) -> AsyncIterator[None]:
    """Bind only this call's credentials and keep BYOK from racing platform Anthropic.

    BYOK must not fall back to OPENAI_API_KEY / ANTHROPIC_API_KEY. Platform completions
    must not run while a BYOK embed has popped those env vars or set litellm.api_key.
    """
    async with _byok_env_lock:
        litellm = _litellm()
        if target.source == 'byok':
            saved_env = {name: os.environ.pop(name) for name in _PLATFORM_ENV if name in os.environ}
            runtime = _byok_runtime_env(target)
        else:
            saved_env = {}
            runtime = _platform_runtime_env(target)
        saved_runtime = {name: os.environ.get(name) for name in runtime}
        os.environ.update(runtime)
        saved_attrs = {name: getattr(litellm, name, None) for name in _LITELLM_KEY_ATTRS}
        for name in _LITELLM_KEY_ATTRS:
            if hasattr(litellm, name):
                setattr(litellm, name, None)
        call_key = (target.auth or {}).get('api_key')
        if call_key and hasattr(litellm, 'api_key'):
            litellm.api_key = call_key
        try:
            yield
        finally:
            for name, value in saved_runtime.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            os.environ.update(saved_env)
            for name, value in saved_attrs.items():
                if hasattr(litellm, name):
                    setattr(litellm, name, value)


def _flatten_completion_content(content: Any) -> str:
    """LiteLLM/Anthropic may return a string or a list of content blocks."""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item)
                continue
            if isinstance(item, Mapping):
                kind = str(item.get('type') or '').lower()
                if kind in {'thinking', 'tool_use', 'tool_result', 'image', 'image_url'}:
                    continue
                text = item.get('text')
                if isinstance(text, str) and text:
                    parts.append(text)
                elif isinstance(text, Mapping):
                    value = text.get('value') or text.get('text')
                    if isinstance(value, str) and value:
                        parts.append(value)
                continue
            text = getattr(item, 'text', None)
            if isinstance(text, str) and text:
                parts.append(text)
        return '\n'.join(parts)
    return str(content)


def _content(content: str | Sequence[TextPart | ImagePart]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({'type': 'text', 'text': part.text})
        else:
            url = f'data:{part.media_type};base64,{part.data_base64}'
            parts.append({'type': 'image_url', 'image_url': {'url': url}})
    return parts


def _preview(messages: Sequence[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == 'user':
            if isinstance(message.content, str):
                return message.content
            return ' '.join(p.text for p in message.content if isinstance(p, TextPart))
    return ''


def _field(item: Any, name: str) -> Any:
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _request_kwargs(target: ResolvedModel, settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        'model': f'{target.provider}/{target.model}',
        'timeout': settings.ai_request_timeout_seconds,
        # Workflows already retry with backoff. LiteLLM retries redo the entire
        # completion/embedding, which doubled gap-analysis and redline latency.
        'num_retries': 0,
        **target.auth,
    }
    kwargs['drop_params'] = True
    if target.api_base:
        kwargs['api_base'] = target.api_base
    return kwargs


def _stop_reason(choice: Any) -> str | None:
    """Normalize LiteLLM/Anthropic finish reasons so a complete reply is not retried."""
    reason = getattr(choice, 'finish_reason', None)
    if reason is None:
        return None
    normalized = str(reason).strip().lower()
    if normalized in {'stop', 'end_turn', 'stop_sequence', 'tool_calls', 'function_call'}:
        return 'stop'
    if normalized in {'length', 'max_tokens'}:
        return 'length'
    return normalized


async def _list_models(target: ResolvedModel, url: str, headers: dict[str, str], timeout: float) -> None:
    async with _without_platform_credentials(target):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:
            raise _translate_error(exc, target) from None
    if response.status_code in {401, 403}:
        raise AIAuthError(f'{target.provider} rejected the credentials.', provider=target.provider)
    if response.status_code == 429:
        raise AIRateLimitError(f'{target.provider} rate limit reached.', provider=target.provider)
    if response.status_code >= 400:
        raise AIProviderError(
            f'{target.provider} request failed: HTTP {response.status_code}', provider=target.provider)


async def check_api_key(target: ResolvedModel, settings: Settings | None = None) -> int | None:
    """Confirm the key is accepted without requiring the selected model to complete a prompt.

    Anthropic and OpenAI expose a models list that returns 401/403 for a bad key and 200 for a
    good one. A completion probe can fail for unrelated reasons (retired model id, max tokens).
    """
    cfg = settings or get_settings()
    api_key = target.auth.get('api_key')
    if target.provider == 'anthropic' and api_key:
        await _list_models(
            target, 'https://api.anthropic.com/v1/models',
            {'x-api-key': api_key, 'anthropic-version': '2023-06-01'},
            cfg.ai_request_timeout_seconds,
        )
        return None
    if target.provider == 'openai' and api_key and not target.api_base:
        await _list_models(
            target, 'https://api.openai.com/v1/models',
            {'Authorization': f'Bearer {api_key}'},
            cfg.ai_request_timeout_seconds,
        )
        return None
    if target.kind == 'embedding':
        return await probe_dimensions(target, cfg)
    await LiteLLMGenerationProvider(target, settings=cfg).generate(
        [ChatMessage('user', 'Reply with OK.')], max_tokens=5, trace_name='ai.probe')
    return None


class LiteLLMGenerationProvider:
    def __init__(self, target: ResolvedModel, *, settings: Settings | None = None) -> None:
        if target.kind != 'generation':
            raise AIConfigurationError('Generation provider needs a generation model')
        self._target = target
        self._settings = settings or get_settings()

    @property
    def model_label(self) -> str:
        return model_label(self._target.provider, self._target.model)

    async def generate(
        self,
        messages: Sequence[ChatMessage],
        *,
        max_tokens: int,
        system: str | None = None,
        temperature: float | None = None,
        cache_system: bool = False,
        throttle: ThrottleTier | None = None,
        throttle_budget: bool = True,
        trace_name: str = 'ai.generate',
        trace_metadata: Mapping[str, object] | None = None,
    ) -> GenerationResult:
        target = self._target
        payload: list[dict[str, Any]] = []
        if system and cache_system and target.provider == 'anthropic':
            payload.append({'role': 'system', 'content': [
                {'type': 'text', 'text': system, 'cache_control': {'type': 'ephemeral'}}]})
        elif system:
            payload.append({'role': 'system', 'content': system})
        payload.extend({'role': m.role, 'content': _content(m.content)} for m in messages)
        kwargs = _request_kwargs(target, self._settings)
        if temperature is not None:
            kwargs['temperature'] = temperature
        if throttle and target.source == 'platform' and target.provider == 'anthropic':
            await throttle_platform_anthropic(
                throttle, _chars(system, messages), self._settings, budget=throttle_budget)
        await _wait(target, self._settings)
        try:
            async with _without_platform_credentials(target):
                response = await _litellm().acompletion(messages=payload, max_tokens=max_tokens, **kwargs)
        except Exception as exc:
            raise _translate_error(exc, target) from None
        choice = response.choices[0]
        usage = getattr(response, 'usage', None)
        message = getattr(choice, 'message', None)
        result = GenerationResult(
            text=_flatten_completion_content(_field(message, 'content') if message is not None else None),
            provider=target.provider,
            model=target.model,
            input_tokens=int(getattr(usage, 'prompt_tokens', 0) or 0),
            output_tokens=int(getattr(usage, 'completion_tokens', 0) or 0),
            stop_reason=_stop_reason(choice),
        )
        record_ai_usage(target, input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                        trace_name=trace_name)
        record_langfuse_generation(
            trace_name,
            model=f'{target.provider}/{target.model}',
            input_data={'system_preview': system or '', 'user_preview': _preview(messages)},
            output_data=result.text,
            metadata={
                **(trace_metadata or {}),
                'provider': target.provider,
                'credential_source': target.source,
                'org_id': target.org_id,
            },
            usage_details={
                'input': result.input_tokens,
                'output': result.output_tokens,
                'total': result.input_tokens + result.output_tokens,
            },
        )
        return result


class LiteLLMEmbeddingProvider:
    """Returns vectors at the pgvector column width; shorter models are zero-padded,
    which preserves cosine similarity, inner product, and L2 distance."""

    def __init__(self, target: ResolvedModel, *, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        width = self._settings.embedding_dimensions
        if target.kind != 'embedding' or not target.dimensions or target.dimensions > width:
            raise AIConfigurationError(f'Embedding model must produce 1..{width} dimensions')
        self._target = target

    @property
    def model_label(self) -> str:
        return model_label(self._target.provider, self._target.model)

    @property
    def vector_space(self) -> str:
        target = self._target
        space = f'{target.provider}/{target.model}@{target.dimensions}'
        return space if target.source == 'platform' else f'{target.org_id}:{space}:{target.api_base or ""}'

    async def embed(self, texts: Sequence[str], *, trace_name: str = 'ai.embed') -> EmbeddingResult:
        target = self._target
        native = int(target.dimensions or 0)
        width = self._settings.embedding_dimensions
        kwargs = _request_kwargs(target, self._settings)
        if target.supports_dimensions:
            kwargs['dimensions'] = native
        vectors: list[list[float]] = []
        total_tokens = 0
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = list(texts[start:start + EMBEDDING_BATCH_SIZE])
            await _wait(target, self._settings)
            try:
                async with _without_platform_credentials(target):
                    response = await _litellm().aembedding(input=batch, **kwargs)
            except Exception as exc:
                raise _translate_error(exc, target) from None
            items = sorted(response.data, key=lambda item: _field(item, 'index') or 0)
            batch_vectors = [list(_field(item, 'embedding')) for item in items]
            if len(batch_vectors) != len(batch) or any(len(v) != native for v in batch_vectors):
                raise AIProviderError(
                    f'{target.provider}/{target.model} did not return {native}-dimension vectors')
            vectors.extend(v + [0.0] * (width - native) for v in batch_vectors)
            batch_tokens = int(_field(getattr(response, 'usage', None) or {}, 'total_tokens') or 0)
            total_tokens += batch_tokens
            record_ai_usage(target, input_tokens=batch_tokens, items=len(batch_vectors), trace_name=trace_name)
            record_embedding_batch(
                name=trace_name,
                model=f'{target.provider}/{target.model}',
                chunk_count=len(batch_vectors),
                usage_tokens=batch_tokens or None,
                preview=batch[0] if batch else None,
            )
        return EmbeddingResult(
            vectors=vectors,
            provider=target.provider,
            model=target.model,
            dimensions=width,
            total_tokens=total_tokens,
        )


async def probe_dimensions(target: ResolvedModel, settings: Settings | None = None) -> int:
    """Embed one short string without a dimension check; return the native width."""
    cfg = settings or get_settings()
    kwargs = _request_kwargs(target, cfg)
    await _wait(target, cfg)
    try:
        async with _without_platform_credentials(target):
            response = await _litellm().aembedding(input=['Reg-AI dimension probe'], **kwargs)
    except Exception as exc:
        raise _translate_error(exc, target) from None
    record_ai_usage(target, items=1, trace_name='ai.probe')
    return len(_field(response.data[0], 'embedding') or [])
