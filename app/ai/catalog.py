"""Model catalog: platform defaults, curated suggestions, and validated custom models.

Any LiteLLM chat/embedding provider is allowed, including self-hosted open-source
endpoints (Ollama, vLLM, LM Studio, any OpenAI-compatible server). Providers that
read local credential files, run models in-process, or are not model APIs are blocked.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from app.ai.types import AIConfigurationError, ModelKind, ModelMapEntry, Purpose
from app.core.config import Settings, get_settings

VISION = 'vision'

BLOCKED_PROVIDERS = frozenset({
    # Local OAuth/device-flow tokens or in-process execution.
    'chatgpt', 'cursor', 'github_copilot', 'custom', 'vllm', 'petals', 'auto_router', 'dotprompt',
    # Agents, observability, vector stores, speech/image-only APIs.
    'a2a', 'a2a_agent', 'litellm_agent', 'langfuse', 'humanloop', 'langgraph', 'langflow', 'manus',
    'cognition', 'pg_vector', 'milvus', 'mongodb', 'valkey', 's3_vectors', 'ragflow', 'aws_polly',
    'deepgram', 'elevenlabs', 'soniox', 'nvidia_riva', 'assemblyai', 'recraft', 'runwayml',
    'stability', 'black_forest_labs', 'fal_ai', 'reducto',
})
# Default to localhost (or have no public default), so an explicit api_base is mandatory.
API_BASE_REQUIRED_PROVIDERS = frozenset({
    'ollama', 'ollama_chat', 'hosted_vllm', 'openai_like', 'custom_openai', 'aiohttp_openai', 'lm_studio',
    'llamafile', 'oobabooga', 'xinference', 'infinity', 'triton', 'lemonade', 'docker_model_runner',
    'litellm_proxy', 'azure', 'azure_ai', 'azure_text', 'databricks',
})
_MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}$')

PROVIDER_LABELS = {
    'anthropic': 'Anthropic', 'openai': 'OpenAI', 'gemini': 'Google Gemini', 'vertex_ai': 'Google Vertex AI',
    'xai': 'xAI Grok', 'azure': 'Azure OpenAI', 'bedrock': 'AWS Bedrock', 'mistral': 'Mistral',
    'cohere': 'Cohere', 'groq': 'Groq', 'together_ai': 'Together AI', 'fireworks_ai': 'Fireworks AI',
    'deepseek': 'DeepSeek', 'openrouter': 'OpenRouter', 'huggingface': 'Hugging Face',
    'ollama': 'Ollama (self-hosted)', 'hosted_vllm': 'vLLM (self-hosted)', 'lm_studio': 'LM Studio',
    'openai_like': 'OpenAI-compatible endpoint', 'voyage': 'Voyage AI', 'nvidia_nim': 'NVIDIA NIM',
}
# Display names only; PROVIDER_LABELS also ranks providers first in model search.
_OTHER_PROVIDER_LABELS = {
    'ai21': 'AI21 Labs', 'azure_ai': 'Azure AI Foundry', 'bedrock_mantle': 'AWS Bedrock Mantle',
    'dashscope': 'Alibaba DashScope', 'deepinfra': 'DeepInfra', 'friendliai': 'FriendliAI', 'gigachat': 'GigaChat',
    'gradient_ai': 'DigitalOcean Gradient AI', 'libertai': 'LibertAI', 'meta_llama': 'Meta Llama API',
    'minimax': 'MiniMax', 'moonshot': 'Moonshot AI', 'nlp_cloud': 'NLP Cloud', 'novita': 'Novita AI',
    'amazon_nova': 'Amazon Nova', 'oci': 'Oracle Cloud (OCI)', 'ovhcloud': 'OVHcloud',
    'publicai': 'PublicAI', 'qwencloud': 'Qwen Cloud',
    'sagemaker': 'AWS SageMaker', 'sambanova': 'SambaNova', 'scx-ai': 'SCX AI', 'tensormesh': 'TensorMesh',
    'v0': 'v0', 'wandb': 'Weights & Biases', 'watsonx': 'IBM watsonx', 'zai': 'Z.ai',
}


def provider_label(provider_id: str) -> str:
    """Human name for a LiteLLM provider id (``featherless_ai`` -> ``Featherless AI``)."""
    known = PROVIDER_LABELS.get(provider_id) or _OTHER_PROVIDER_LABELS.get(provider_id)
    if known:
        return known
    words = [w for w in re.split(r'[_\-]+', provider_id) if w]
    return ' '.join('AI' if w == 'ai' else w[:1].upper() + w[1:] for w in words) or provider_id


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    requires_api_base: bool = False


@dataclass(frozen=True)
class ModelSpec:
    provider: str
    model: str
    label: str
    kind: ModelKind
    capabilities: frozenset[str] = frozenset()
    # Embeddings: native vector width. Shorter vectors are zero-padded to EMBEDDING_DIMENSIONS.
    dimensions: int | None = None
    # Provider accepts ``dimensions=EMBEDDING_DIMENSIONS`` (Matryoshka models).
    supports_dimensions: bool = False
    api_base: str | None = None
    curated: bool = True
    is_default: bool = False


_V = frozenset({VISION})
_SUGGESTED: tuple[ModelSpec, ...] = (
    ModelSpec('openai', 'gpt-5', 'GPT-5', 'generation', _V),
    ModelSpec('gemini', 'gemini-2.5-pro', 'Gemini 2.5 Pro', 'generation', _V),
    ModelSpec('xai', 'grok-4', 'Grok 4', 'generation', _V),
    ModelSpec('mistral', 'mistral-large-latest', 'Mistral Large', 'generation'),
    ModelSpec('groq', 'llama-3.3-70b-versatile', 'Llama 3.3 70B (Groq)', 'generation'),
    ModelSpec('together_ai', 'meta-llama/Llama-3.3-70B-Instruct-Turbo', 'Llama 3.3 70B (Together)', 'generation'),
    ModelSpec('ollama', 'llama3.3', 'Llama 3.3 (self-hosted Ollama)', 'generation'),
    ModelSpec('gemini', 'gemini-embedding-001', 'Gemini embedding 001', 'embedding',
              dimensions=3072, supports_dimensions=True),
    ModelSpec('ollama', 'nomic-embed-text', 'nomic-embed-text (self-hosted Ollama)', 'embedding', dimensions=768),
)


@dataclass(frozen=True)
class ModelPage:
    total: int
    items: tuple[ModelSpec, ...]


@dataclass(frozen=True)
class ProviderSummary:
    provider: ProviderSpec
    generation_models: int
    embedding_models: int


_FEATURED_PROVIDERS = {pid: rank for rank, pid in enumerate(PROVIDER_LABELS)}


class ModelCatalog:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provider_ids: Callable[[], Iterable[str]] | None = None,
        model_entries: Callable[[], Iterable[ModelMapEntry]] | None = None,
    ) -> None:
        self._settings = s = settings or get_settings()
        self._provider_ids_source = provider_ids
        self._provider_ids: frozenset[str] | None = None
        self._model_entries_source = model_entries
        self._available: tuple[ModelSpec, ...] | None = None
        self.storage_dimensions = s.embedding_dimensions
        anthropic = dict.fromkeys((
            s.resolved_anthropic_redline_model, s.resolved_anthropic_gap_analysis_model,
            s.anthropic_sonnet_model, s.anthropic_opus_model,
        ))
        self._defaults: dict[Purpose, ModelSpec] = {
            'redline': ModelSpec('anthropic', s.resolved_anthropic_redline_model,
                                 s.resolved_anthropic_redline_model, 'generation', _V, is_default=True),
            'analysis': ModelSpec('anthropic', s.resolved_anthropic_gap_analysis_model,
                                  s.resolved_anthropic_gap_analysis_model, 'generation', _V, is_default=True),
            'embedding': ModelSpec('openai', s.openai_embedding_model, s.openai_embedding_model, 'embedding',
                                   dimensions=s.embedding_dimensions, supports_dimensions=True, is_default=True),
        }
        suggested = [self._defaults['redline'], self._defaults['analysis'], self._defaults['embedding']]
        suggested.extend(ModelSpec('anthropic', name, name, 'generation', _V) for name in anthropic
                         if name not in {self._defaults['redline'].model, self._defaults['analysis'].model})
        suggested.extend(_SUGGESTED)
        unique: dict[tuple[str, str, str], ModelSpec] = {}
        for m in suggested:
            if m.kind != 'embedding' or (m.dimensions or 0) <= self.storage_dimensions:
                unique.setdefault((m.provider, m.model, m.kind), m)
        self._suggested = tuple(unique.values())

    def _known_provider_ids(self) -> frozenset[str]:
        if self._provider_ids is None:
            if self._provider_ids_source is None:
                from app.ai.litellm_adapter import supported_provider_ids

                self._provider_ids_source = supported_provider_ids
            self._provider_ids = frozenset(self._provider_ids_source()) - BLOCKED_PROVIDERS
        return self._provider_ids

    def providers(self) -> tuple[ProviderSpec, ...]:
        return tuple(self.provider(pid) for pid in sorted(
            self._known_provider_ids(), key=lambda pid: (pid not in PROVIDER_LABELS, pid)))

    def provider(self, provider_id: str) -> ProviderSpec:
        if provider_id not in self._known_provider_ids():
            raise AIConfigurationError(f'Unsupported AI provider: {provider_id}')
        return ProviderSpec(provider_id, provider_label(provider_id),
                            provider_id in API_BASE_REQUIRED_PROVIDERS)

    def suggested(self, *, kind: ModelKind | None = None) -> tuple[ModelSpec, ...]:
        return tuple(m for m in self._suggested if kind is None or m.kind == kind)

    def validate_model_name(self, model: str) -> str:
        if not _MODEL_RE.fullmatch(model or ''):
            raise AIConfigurationError('Model name contains unsupported characters')
        return model

    def available_models(self) -> tuple[ModelSpec, ...]:
        """Every chat and embedding model in LiteLLM's bundled map, Reg-AI defaults first.

        Embeddings wider than the vector column, or with no published width, are left out.
        """
        if self._available is None:
            if self._model_entries_source is None:
                from app.ai.litellm_adapter import model_map_entries

                self._model_entries_source = model_map_entries
            known = self._known_provider_ids()
            unique: dict[tuple[str, str, str], ModelSpec] = {
                (m.provider, m.model, m.kind): m for m in self._suggested if m.provider in known
            }
            for entry in self._model_entries_source():
                if entry.provider not in known or not _MODEL_RE.fullmatch(entry.model):
                    continue
                if entry.kind == 'embedding' and not 0 < (entry.dimensions or 0) <= self.storage_dimensions:
                    continue
                unique.setdefault((entry.provider, entry.model, entry.kind), ModelSpec(
                    entry.provider, entry.model, entry.model, entry.kind,
                    _V if entry.vision else frozenset(), dimensions=entry.dimensions, curated=False,
                ))
            self._available = tuple(sorted(unique.values(), key=lambda m: (
                not m.is_default,
                _FEATURED_PROVIDERS.get(m.provider, len(_FEATURED_PROVIDERS)),
                m.provider,
                m.kind,
                m.model,
            )))
        return self._available

    def search_models(
        self,
        query: str = '',
        *,
        kind: ModelKind | None = None,
        provider: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> ModelPage:
        """Case-insensitive match of every word against provider id, provider name, and model."""
        terms = query.lower().split()
        matched = [
            m for m in self.available_models()
            if (kind is None or m.kind == kind)
            and (provider is None or m.provider == provider)
            and all(t in f'{m.provider} {self.provider(m.provider).label} {m.model}'.lower() for t in terms)
        ]
        return ModelPage(len(matched), tuple(matched[offset:offset + limit]))

    def provider_summaries(self, query: str = '', *, kind: ModelKind | None = None) -> tuple[ProviderSummary, ...]:
        """Providers with at least one model matching ``query`` (same rules as ``search_models``),
        alphabetical by provider name."""
        terms = query.lower().split()
        specs: dict[str, ProviderSpec] = {}
        counts: dict[str, dict[str, int]] = {}
        for m in self.available_models():
            if kind is not None and m.kind != kind:
                continue
            spec = specs.get(m.provider) or specs.setdefault(m.provider, self.provider(m.provider))
            if not all(t in f'{m.provider} {spec.label} {m.model}'.lower() for t in terms):
                continue
            by_kind = counts.setdefault(m.provider, {'generation': 0, 'embedding': 0})
            by_kind[m.kind] += 1
        return tuple(sorted(
            (ProviderSummary(specs[pid], c['generation'], c['embedding']) for pid, c in counts.items()),
            key=lambda s: (s.provider.label.casefold(), s.provider.id),
        ))

    def platform_default(self, purpose: Purpose) -> ModelSpec:
        """Anthropic for redline/analysis and OpenAI for embeddings, exactly as today."""
        return self._defaults[purpose]

    def default_roles(
        self, provider: str, model: str, kind: ModelKind,
    ) -> tuple[Literal['analysis', 'redline', 'embedding'], ...]:
        """Jobs this model is the Reg-AI default for: analysis, redline, and/or embedding."""
        roles: list[Literal['analysis', 'redline', 'embedding']] = []
        if kind == 'generation':
            for purpose in ('analysis', 'redline'):
                default = self._defaults[purpose]
                if (default.provider, default.model) == (provider, model):
                    roles.append(purpose)
        elif (self._defaults['embedding'].provider, self._defaults['embedding'].model) == (provider, model):
            roles.append('embedding')
        return tuple(roles)

    def resolve(
        self,
        provider: str,
        model: str,
        kind: ModelKind,
        *,
        api_base: str | None = None,
        dimensions: int | None = None,
        require_dimensions: bool = True,
    ) -> ModelSpec:
        """Return a curated spec, or validate and build a custom one.

        ``require_dimensions=False`` is only for probing an unknown embedding width.
        """
        spec = self.provider(provider)
        if not _MODEL_RE.fullmatch(model or ''):
            raise AIConfigurationError('Model name contains unsupported characters')
        if spec.requires_api_base and not api_base:
            raise AIConfigurationError(f'{spec.label} needs an endpoint URL (api_base)')
        if not api_base:
            for curated in self._suggested:
                if (curated.provider, curated.model, curated.kind) == (provider, model, kind):
                    if dimensions and curated.dimensions and dimensions != curated.dimensions:
                        raise AIConfigurationError(f'{curated.label} returns {curated.dimensions} dimensions')
                    return curated
        if kind == 'embedding':
            if dimensions is None and not require_dimensions:
                pass
            elif not dimensions or not 1 <= dimensions <= self.storage_dimensions:
                raise AIConfigurationError(
                    f'Embedding dimensions must be between 1 and {self.storage_dimensions}')
        elif dimensions:
            raise AIConfigurationError('Dimensions apply to embedding models only')
        return ModelSpec(provider, model, model, kind, dimensions=dimensions, api_base=api_base, curated=False)


@lru_cache
def get_model_catalog() -> ModelCatalog:
    return ModelCatalog()
