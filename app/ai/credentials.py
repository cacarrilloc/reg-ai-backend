"""Resolve which model runs for an org and whose credentials pay for it."""

from __future__ import annotations

from dataclasses import replace

from app.ai.catalog import ModelCatalog, ModelSpec, get_model_catalog
from app.ai.crypto import key_hint
from app.ai.network import validate_api_base
from app.ai.store import AISettingsRecord, AISettingsStore
from app.ai.types import AIConfigurationError, AICredentialMissing, Purpose, ResolvedModel
from app.core.config import Settings, get_settings

MODE_PLATFORM = 'platform'
MODE_BYOK = 'byok'

CREDENTIAL_FIELDS = frozenset({
    'api_key', 'api_version', 'organization', 'project_id',
    'aws_access_key_id', 'aws_secret_access_key', 'aws_session_token', 'aws_region_name',
    'vertex_project', 'vertex_location', 'vertex_credentials',
})
# Providers authenticated by cloud credentials instead of an api_key.
CLOUD_AUTH_FIELDS: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        ('bedrock', 'bedrock_mantle', 'sagemaker', 'sagemaker_chat', 'sagemaker_nova'),
        frozenset({'aws_access_key_id', 'aws_secret_access_key', 'aws_region_name'}),
    ),
    **dict.fromkeys(
        ('vertex_ai', 'vertex_ai_beta'),
        frozenset({'vertex_credentials', 'vertex_project', 'vertex_location'}),
    ),
}
# Sent to keyless self-hosted endpoints so LiteLLM never reads OPENAI_API_KEY etc. from env.
KEYLESS_PLACEHOLDER = 'reg-ai-no-key'


KEYLESS_HINT = 'no key'


def normalize_credential(
    provider: str, fields: dict[str, str], *, keyless: bool = False,
) -> tuple[dict[str, str], str]:
    """Validate a credential bundle; return (fields, display hint).

    ``keyless`` allows an empty bundle for self-hosted endpoints that need no API key.
    """
    cleaned = {k: v.strip() for k, v in fields.items() if v and v.strip()}
    unknown = sorted(set(cleaned) - CREDENTIAL_FIELDS)
    if unknown:
        raise AIConfigurationError(f'Unsupported credential fields: {", ".join(unknown)}')
    if keyless and provider not in CLOUD_AUTH_FIELDS and not cleaned.get('api_key'):
        return cleaned, KEYLESS_HINT
    required = CLOUD_AUTH_FIELDS.get(provider, frozenset({'api_key'}))
    missing = sorted(required - set(cleaned))
    if missing:
        raise AIConfigurationError(f'Missing credential fields: {", ".join(missing)}')
    secret = cleaned.get('api_key') or cleaned.get('aws_access_key_id') or cleaned.get('vertex_project', '')
    return cleaned, key_hint(secret)


def platform_api_key(provider: str, settings: Settings) -> str | None:
    return {
        'anthropic': settings.anthropic_api_key,
        'openai': settings.openai_api_key,
    }.get(provider)


class CredentialResolver:
    """Rules:

    * ``org_id=None`` (shared feed, newsletter) always runs on platform keys.
    * No settings row, or ``mode=platform``: platform defaults (Anthropic generation,
      OpenAI embeddings) on platform keys.
    * ``mode=byok``: the org's models and credentials. A workflow still on the Reg-AI default
      (the one the user did not pick at registration) uses platform keys until they choose their own.
    * Embeddings always use the org's saved embedding profile once one exists.
    """

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        catalog: ModelCatalog | None = None,
        store: AISettingsStore | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._catalog = catalog or get_model_catalog()
        self._store = store or AISettingsStore()

    def for_org(
        self, org_id: str | None, purpose: Purpose, *, platform_model: str | None = None
    ) -> ResolvedModel:
        """``platform_model`` pins the Anthropic model a workflow uses in platform mode."""
        record = self._store.load_settings(org_id) if org_id else None
        byok = record is not None and record.mode == MODE_BYOK
        spec = self._select(record, purpose)
        if byok and org_id and self._store.load_credential(org_id, spec.provider) is None:
            leftover = self._catalog.platform_default(purpose)
            if spec.provider == leftover.provider and spec.model == leftover.model:
                spec = leftover
                byok = False
        if platform_model and not byok and purpose != 'embedding':
            spec = replace(spec, model=platform_model, label=platform_model)
        return self.bind(spec, org_id=org_id, byok=byok)

    def bind(
        self, spec: ModelSpec, *, org_id: str | None, byok: bool, auth: dict[str, str] | None = None,
    ) -> ResolvedModel:
        """Attach the paying credential to a model spec.

        ``auth`` is a key that is not stored yet (verified before saving); otherwise BYOK loads the stored key.
        """
        api_base = validate_api_base(spec.api_base, self._settings)
        label = self._catalog.provider(spec.provider).label
        if byok:
            if auth is None:
                if org_id is None:
                    raise AIConfigurationError('BYOK requires an organization')
                auth = self._store.load_credential(org_id, spec.provider)
            if auth is None and not (api_base and spec.provider not in CLOUD_AUTH_FIELDS):
                raise AICredentialMissing(f'Add your {label} credentials in AI settings.')
            auth = dict(auth or {})
            if spec.provider not in CLOUD_AUTH_FIELDS:
                auth.setdefault('api_key', KEYLESS_PLACEHOLDER)
        else:
            if api_base or not spec.is_default:
                raise AIConfigurationError('Platform mode only runs Reg-AI default models')
            platform_key = platform_api_key(spec.provider, self._settings)
            if not platform_key:
                raise AICredentialMissing(f'Reg-AI {label} key is not configured.')
            auth = {'api_key': platform_key}
        return ResolvedModel(
            provider=spec.provider,
            model=spec.model,
            kind=spec.kind,
            source='byok' if byok else 'platform',
            org_id=org_id,
            auth=auth,
            api_base=api_base,
            dimensions=spec.dimensions,
            supports_dimensions=spec.supports_dimensions,
        )

    def _select(self, record: AISettingsRecord | None, purpose: Purpose) -> ModelSpec:
        if record is None:
            return self._catalog.platform_default(purpose)
        if purpose == 'embedding':
            return self._catalog.resolve(
                record.embedding_provider, record.embedding_model, 'embedding',
                api_base=record.embedding_api_base, dimensions=record.embedding_dimensions,
            )
        if record.mode == MODE_BYOK and purpose == 'analysis' and record.analysis_provider and record.analysis_model:
            return self._catalog.resolve(
                record.analysis_provider, record.analysis_model, 'generation',
                api_base=record.analysis_api_base,
            )
        if record.mode == MODE_BYOK and purpose == 'redline' and record.generation_provider and record.generation_model:
            return self._catalog.resolve(
                record.generation_provider, record.generation_model, 'generation',
                api_base=record.generation_api_base,
            )
        return self._catalog.platform_default(purpose)
