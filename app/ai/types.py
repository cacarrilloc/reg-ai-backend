"""Provider-neutral AI types. Workflows depend on these, never on an SDK or LiteLLM."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

ModelKind = Literal['generation', 'embedding']
# analysis = judgment-heavy generation; redline = drafting; embedding = vector search.
ThrottleTier = Literal['opus', 'sonnet']
Purpose = Literal['analysis', 'redline', 'embedding']
CredentialSource = Literal['platform', 'byok']


@dataclass(frozen=True)
class TextPart:
    text: str


@dataclass(frozen=True)
class ImagePart:
    media_type: str
    data_base64: str


@dataclass(frozen=True)
class ChatMessage:
    role: Literal['user', 'assistant']
    content: str | Sequence[TextPart | ImagePart]


@dataclass(frozen=True)
class ModelMapEntry:
    """One model from LiteLLM's bundled model map."""

    provider: str
    model: str
    kind: ModelKind
    dimensions: int | None = None
    vision: bool = False
    max_input_tokens: int | None = None


@dataclass(frozen=True)
class ResolvedModel:
    """A model bound to the credential that pays for it."""

    provider: str
    model: str
    kind: ModelKind
    source: CredentialSource
    org_id: str | None
    # LiteLLM auth kwargs (api_key, aws_*, vertex_*, api_version, ...). Always explicit.
    auth: Mapping[str, str] = field(repr=False, default_factory=dict)
    api_base: str | None = None
    dimensions: int | None = None
    supports_dimensions: bool = False


@dataclass(frozen=True)
class GenerationResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None

    @property
    def truncated(self) -> bool:
        return self.stop_reason in {'length', 'max_tokens'}


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    provider: str
    model: str
    dimensions: int
    total_tokens: int = 0


def model_label(provider: str, model: str) -> str:
    """Stored/displayed model name. Anthropic keeps the bare name used before BYOK."""
    return model if provider == 'anthropic' else f'{provider}/{model}'


class LLMProvider(Protocol):
    @property
    def model_label(self) -> str: ...

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
    ) -> GenerationResult: ...


class EmbeddingProvider(Protocol):
    @property
    def model_label(self) -> str: ...

    @property
    def vector_space(self) -> str:
        """Equal values mean vectors are directly comparable."""
        ...

    async def embed(self, texts: Sequence[str], *, trace_name: str = 'ai.embed') -> EmbeddingResult: ...


class AIError(RuntimeError):
    """Base error. Messages are safe to show users and never contain API keys."""


class AIConfigurationError(AIError):
    """Unknown provider/model, or an invalid selection for the requested use."""


class AICredentialMissing(AIError):
    """No usable key. BYOK orgs never fall back to platform keys."""


class AIEncryptionUnavailable(AIError):
    """AI_CREDENTIAL_ENCRYPTION_KEYS is not configured or cannot decrypt."""


class AIProviderError(AIError):
    retryable: bool = True

    def __init__(self, message: str, *, provider: str = '') -> None:
        super().__init__(message)
        self.provider = provider


class AIAuthError(AIProviderError):
    retryable = False


class AIBillingError(AIProviderError):
    retryable = False


class AIRateLimitError(AIProviderError):
    pass
