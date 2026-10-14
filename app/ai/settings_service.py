"""Validated reads/writes of org AI settings (Organization Settings and org registration)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

import structlog

from app.ai.catalog import ModelCatalog, ModelSpec, get_model_catalog
from app.ai.credentials import CLOUD_AUTH_FIELDS, MODE_BYOK, MODE_PLATFORM, CredentialResolver, normalize_credential
from app.ai.network import validate_api_base
from app.ai.reembedding import ReembedInProgress, ReembedJob, ReembedStore, visible_job
from app.ai.store import AISettingsRecord, AISettingsStore, CredentialStatus
from app.ai.types import (
    AIAuthError,
    AIBillingError,
    AIConfigurationError,
    AICredentialMissing,
    AIError,
    AIProviderError,
    AIRateLimitError,
    ModelKind,
    model_label,
)
from app.core.config import get_settings

logger = structlog.get_logger(__name__)

# A new key gets one short test call before it is stored; a slow provider must not hang the save.
KEY_CHECK_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class AIModeOption:
    id: str
    label: str
    description: str
    billed_to: str
    is_default: bool = False


MODE_OPTIONS: tuple[AIModeOption, ...] = (
    AIModeOption(
        MODE_PLATFORM,
        'Use Reg-AI default models',
        'Analysis uses a stronger Claude model. Redlines use a faster, cheaper Claude model. '
        'Embeddings use OpenAI. All on Reg-AI keys.',
        billed_to='reg_ai',
        is_default=True,
    ),
    AIModeOption(
        MODE_BYOK,
        'Use my own provider and keys',
        'Choose any provider and model, including self-hosted open-source models. Usage is billed by your provider.',
        billed_to='customer',
    ),
)
MODE_LABELS = {option.id: option.label for option in MODE_OPTIONS}


class EmbeddingProfileLocked(AIConfigurationError):
    """Changing the embedding model requires re-embedding every stored vector."""


class AICredentialInvalid(AIConfigurationError):
    """The provider did not accept a test call with a new key, so the key is not stored."""


def _key_rejected_message(label: str, exc: AIProviderError) -> str:
    not_saved = 'The key was not saved.'
    if isinstance(exc, AIAuthError):
        return f'{label} rejected this API key: it is invalid or wrong. {not_saved} Check the key and try again.'
    if isinstance(exc, AIBillingError):
        return (f'{label} refused a test request with this key because of billing or quota. '
                f'Add credit to the {label} account, then try again. {not_saved}')
    if isinstance(exc, AIRateLimitError):
        return f'{label} is rate-limiting this key, so it could not be verified. Try again in a minute. {not_saved}'
    return f'{label} did not respond correctly to a test request with this key ({exc}). {not_saved}'


@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model: str
    api_base: str | None = None
    dimensions: int | None = None


@dataclass(frozen=True)
class MissingCredential:
    provider: str
    provider_name: str
    purpose: Literal['generation', 'embedding', 'analysis', 'redline']


@dataclass(frozen=True)
class OrgAIView:
    mode: str
    generation: ModelSelection
    analysis: ModelSelection
    embedding: ModelSelection
    configured: bool
    credentials: list[CredentialStatus]
    missing_credentials: tuple[MissingCredential, ...] = ()
    # Stored vectors come from ``embedding``; another embedding model means re-embedding them.
    embedded_chunks: int = 0
    reembed: ReembedJob | None = None

    @property
    def embedding_locked(self) -> bool:
        return self.embedded_chunks > 0


@dataclass(frozen=True)
class OwnKeyChoice:
    """The key an org enters while registering; validated before the org is created."""

    provider: str
    model: str
    kind: ModelKind
    fields: dict[str, str]
    api_base: str | None = None
    dimensions: int | None = None
    # ``analysis`` or ``redline``.
    role: str | None = None


class RegAIBillingNotAccepted(AIConfigurationError):
    """Reg-AI default models are billed by Reg-AI; the user must accept that first."""


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    dimensions: int | None = None
    detail: str | None = None


def _selection(spec: ModelSpec) -> ModelSelection:
    return ModelSelection(spec.provider, spec.model, spec.api_base, spec.dimensions)


def _settings_record(
    org_id: str,
    mode: str,
    generation_spec: ModelSpec | None,
    embedding_spec: ModelSpec,
    analysis_spec: ModelSpec | None = None,
) -> AISettingsRecord:
    return AISettingsRecord(
        auth_org_id=org_id,
        mode=mode,
        generation_provider=generation_spec.provider if generation_spec else None,
        generation_model=generation_spec.model if generation_spec else None,
        generation_api_base=generation_spec.api_base if generation_spec else None,
        analysis_provider=analysis_spec.provider if analysis_spec else None,
        analysis_model=analysis_spec.model if analysis_spec else None,
        analysis_api_base=analysis_spec.api_base if analysis_spec else None,
        embedding_provider=embedding_spec.provider,
        embedding_model=embedding_spec.model,
        embedding_api_base=embedding_spec.api_base,
        embedding_dimensions=int(embedding_spec.dimensions or 0),
    )


class OrgAISettingsService:
    def __init__(
        self,
        *,
        store: AISettingsStore | None = None,
        catalog: ModelCatalog | None = None,
        resolver: CredentialResolver | None = None,
        reembed_jobs: ReembedStore | None = None,
        dispatch_reembed: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store or AISettingsStore()
        self._catalog = catalog or get_model_catalog()
        self._resolver = resolver or CredentialResolver(catalog=self._catalog, store=self._store)
        self._jobs = reembed_jobs or ReembedStore()
        # Schedules the background re-embed; only request handlers that can run one pass it.
        self._dispatch_reembed = dispatch_reembed

    def view(self, org_id: str) -> OrgAIView:
        record = self._store.load_settings(org_id)
        credentials = self._store.credential_statuses(org_id)
        embedded_chunks = self._store.embedded_chunk_count(org_id)
        reembed = visible_job(self._jobs.latest(org_id))
        generation = _selection(self._catalog.platform_default('redline'))
        analysis = _selection(self._catalog.platform_default('analysis'))
        embedding = _selection(self._catalog.platform_default('embedding'))
        if record is None:
            return OrgAIView(MODE_PLATFORM, generation, analysis, embedding, False, credentials,
                             embedded_chunks=embedded_chunks, reembed=reembed)
        if record.mode == MODE_BYOK and record.generation_provider and record.generation_model:
            generation = ModelSelection(record.generation_provider, record.generation_model,
                                        record.generation_api_base)
        if record.mode == MODE_BYOK and record.analysis_provider and record.analysis_model:
            analysis = ModelSelection(record.analysis_provider, record.analysis_model, record.analysis_api_base)
        embedding = ModelSelection(record.embedding_provider, record.embedding_model,
                                   record.embedding_api_base, record.embedding_dimensions)
        missing: tuple[MissingCredential, ...] = ()
        if record.mode == MODE_BYOK:
            stored = {c.provider for c in credentials}
            same_chat = (analysis.provider, analysis.model) == (generation.provider, generation.model)
            selections: tuple[tuple[Literal['generation', 'redline', 'analysis', 'embedding'], ModelSelection], ...] = (
                (('generation', generation),) if same_chat else (('redline', generation), ('analysis', analysis))
            ) + (('embedding', embedding),)
            missing = tuple(
                MissingCredential(sel.provider, self._catalog.provider(sel.provider).label, purpose)
                for purpose, sel in selections
                if sel.provider not in stored
                and not (sel.api_base and sel.provider not in CLOUD_AUTH_FIELDS)
                and not self._is_unselected_platform_default(purpose, sel)
            )
        return OrgAIView(record.mode, generation, analysis, embedding, True, credentials, missing,
                         embedded_chunks=embedded_chunks, reembed=reembed)

    def _is_unselected_platform_default(
        self, purpose: Literal['generation', 'embedding', 'analysis', 'redline'], selection: ModelSelection,
    ) -> bool:
        """True when this workflow was left on the Reg-AI default because the user picked the other one."""
        default_purpose = {
            'analysis': 'analysis', 'embedding': 'embedding', 'generation': 'redline', 'redline': 'redline',
        }[purpose]
        default = self._catalog.platform_default(default_purpose)  # type: ignore[arg-type]
        return selection.provider == default.provider and selection.model == default.model

    def _active_job(self, org_id: str) -> ReembedJob | None:
        job = visible_job(self._jobs.latest(org_id))
        return job if job and job.active else None

    def _resolve(self, kind: ModelKind, selection: ModelSelection, *, require_dimensions: bool = True) -> ModelSpec:
        return self._catalog.resolve(
            selection.provider, selection.model, kind,
            api_base=validate_api_base(selection.api_base),
            dimensions=selection.dimensions,
            require_dimensions=require_dimensions,
        )

    def _with_provider_endpoint(self, org_id: str, selection: ModelSelection | None) -> ModelSelection | None:
        """Self-hosted providers: default to the endpoint saved with the provider key."""
        if selection is None or selection.api_base:
            return selection
        for status in self._store.credential_statuses(org_id):
            if status.provider == selection.provider and status.api_base:
                return replace(selection, api_base=status.api_base)
        return selection

    def update(
        self,
        org_id: str,
        *,
        mode: str,
        generation: ModelSelection | None,
        embedding: ModelSelection | None,
        actor_id: str,
        accept_billing: bool = False,
        reembed: bool = False,
        analysis: ModelSelection | None = None,
    ) -> OrgAIView:
        """Save the org's models. A new embedding model on existing vectors needs ``reembed=True``:
        the switch then happens in the background once every document is re-embedded."""
        if mode == MODE_PLATFORM and not accept_billing:
            raise RegAIBillingNotAccepted(
                'Reg-AI default models are billed by Reg-AI. Accept the usage and billing terms to use them.')
        default_generation = self._catalog.platform_default('redline')
        default_embedding = self._catalog.platform_default('embedding')
        existing = self._store.load_settings(org_id)
        if mode == MODE_BYOK:
            generation = self._with_provider_endpoint(org_id, generation)
            embedding = self._with_provider_endpoint(org_id, embedding)
            analysis = self._with_provider_endpoint(org_id, analysis)
        if mode == MODE_PLATFORM:
            if generation is not None or analysis is not None:
                raise AIConfigurationError('Platform mode uses the Reg-AI default generation models')
            embedding_spec = self._resolve('embedding', embedding) if embedding else default_embedding
            if not embedding_spec.is_default:
                raise AIConfigurationError('Platform mode uses the Reg-AI default embedding model')
            generation_spec = None
            analysis_spec = None
        elif mode == MODE_BYOK:
            generation_spec = self._resolve('generation', generation) if generation else default_generation
            embedding_spec = self._resolve('embedding', embedding) if embedding else default_embedding
            if analysis is not None:
                analysis_spec = self._resolve('generation', analysis)
            elif existing and existing.analysis_provider and existing.analysis_model:
                analysis_spec = self._catalog.resolve(
                    existing.analysis_provider, existing.analysis_model, 'generation',
                    api_base=existing.analysis_api_base,
                )
            else:
                analysis_spec = None
        else:
            raise AIConfigurationError(f'Unknown AI mode: {mode}')

        if self._active_job(org_id):
            raise ReembedInProgress(
                'Documents are being re-embedded for a model switch. Change models after it finishes.')

        current = (
            (existing.embedding_provider, existing.embedding_model, existing.embedding_api_base,
             existing.embedding_dimensions) if existing
            else (default_embedding.provider, default_embedding.model, default_embedding.api_base,
                  default_embedding.dimensions)
        )
        changes_embedding = current != (
            embedding_spec.provider, embedding_spec.model, embedding_spec.api_base, embedding_spec.dimensions)
        embedded_chunks = self._store.embedded_chunk_count(org_id) if changes_embedding else 0
        if embedded_chunks and not reembed:
            raise EmbeddingProfileLocked(
                f'Your {embedded_chunks} document chunks are embedded with {model_label(current[0], current[1])}. '
                'Switching the embedding model re-embeds them first.')

        if generation_spec is not None:
            stored = {c.provider for c in self._store.credential_statuses(org_id)}
            # Omitting a selection fills in the platform default and still requires that key.
            # Naming the platform default explicitly leaves that job on Reg-AI's key.
            required = [
                spec for spec, explicit in (
                    (generation_spec, generation is not None),
                    (embedding_spec, embedding is not None),
                    (analysis_spec, analysis is not None),
                )
                if spec is not None and not (explicit and spec.is_default)
            ]
            missing = sorted({
                spec.provider for spec in required
                if spec.provider not in stored
                and not (spec.api_base and spec.provider not in CLOUD_AUTH_FIELDS)
            })
            if missing:
                labels = ', '.join(self._catalog.provider(p).label for p in missing)
                raise AICredentialMissing(f'Add credentials for {labels} before selecting these models.')

        record = _settings_record(org_id, mode, generation_spec, embedding_spec, analysis_spec)
        if embedded_chunks:
            self._start_reembed(record, total=embedded_chunks, actor_id=actor_id)
        else:
            self._store.save_settings(record, actor_id=actor_id)
        if mode == MODE_PLATFORM:
            logger.info('ai_reg_ai_billing_accepted', org_id=org_id, actor_id=actor_id)
        return self.view(org_id)

    def _start_reembed(self, target: AISettingsRecord, *, total: int, actor_id: str) -> None:
        if self._dispatch_reembed is None:
            raise AIConfigurationError('Re-embedding can only be started from Organization Settings.')
        job = self._jobs.create(target.auth_org_id, target, total=total, actor_id=actor_id)
        logger.info('ai_reembed_started', org_id=target.auth_org_id, job_id=job.id, actor_id=actor_id,
                    chunks=total, embedding_provider=target.embedding_provider,
                    embedding_model=target.embedding_model)
        self._dispatch_reembed(job.id)

    def _save(
        self,
        org_id: str,
        mode: str,
        generation_spec: ModelSpec | None,
        embedding_spec: ModelSpec,
        *,
        actor_id: str,
        analysis_spec: ModelSpec | None = None,
    ) -> None:
        self._store.save_settings(
            _settings_record(org_id, mode, generation_spec, embedding_spec, analysis_spec), actor_id=actor_id)

    def apply_workflow_choices(
        self, org_id: str, choices: list[OwnKeyChoice], *, actor_id: str,
    ) -> OrgAIView:
        """Store each registration key, then set the analysis model and the redline model separately.

        A job with no choice stays on that job's Reg-AI default (billed by Reg-AI until the org
        picks its own model).
        """
        analysis_spec: ModelSpec | None = None
        generation_spec: ModelSpec | None = None
        embedding_spec = self._catalog.platform_default('embedding')
        saw_analysis = False
        saw_redline = False
        saw_embedding = False
        for choice in choices:
            if choice.fields:
                self.transfer_own_key_to_org(org_id, choice, actor_id=actor_id)
            spec = self._catalog.resolve(
                choice.provider, choice.model, choice.kind,
                api_base=validate_api_base(choice.api_base), dimensions=choice.dimensions)
            if self._store.load_credential(org_id, choice.provider) is not None:
                self.set_model_enabled(org_id, choice.provider, choice.model, enabled=True, actor_id=actor_id)
            if choice.role == 'analysis':
                analysis_spec = spec
                saw_analysis = True
            elif choice.role == 'redline':
                generation_spec = spec
                saw_redline = True
            elif choice.role == 'embedding':
                embedding_spec = spec
                saw_embedding = True
        if not saw_analysis and not saw_redline and not saw_embedding:
            raise AIConfigurationError('Choose a model for analysis, redlines, or embeddings.')
        # A chat job the user did not pick stays on that job's Reg-AI default.
        # An embedding-only key does not write a chat model.
        if saw_analysis or saw_redline:
            if not saw_analysis:
                analysis_spec = self._catalog.platform_default('analysis')
            if not saw_redline:
                generation_spec = self._catalog.platform_default('redline')
        self._save(
            org_id, MODE_BYOK, generation_spec, embedding_spec, actor_id=actor_id, analysis_spec=analysis_spec)
        return self.view(org_id)

    def use_default_models(
        self, org_id: str, *, actor_id: str, accept_billing: bool = False, reembed: bool = False,
    ) -> OrgAIView:
        """The "Use Reg-AI default models" option: platform models on Reg-AI keys, billed by Reg-AI."""
        return self.update(org_id, mode=MODE_PLATFORM, generation=None, embedding=None,
                           actor_id=actor_id, accept_billing=accept_billing, reembed=reembed)

    def validate_own_key(self, choice: OwnKeyChoice) -> ModelSpec:
        """Everything that can be checked before the organization exists."""
        spec = self._catalog.provider(choice.provider)
        endpoint = validate_api_base(choice.api_base)
        if spec.requires_api_base and not endpoint:
            raise AIConfigurationError(f'{spec.label} needs an endpoint URL (api_base)')
        normalize_credential(choice.provider, choice.fields, keyless=bool(endpoint))
        model = self._catalog.resolve(choice.provider, choice.model, choice.kind,
                                      api_base=endpoint, dimensions=choice.dimensions)
        self._store.ensure_encryption_available()
        return model

    def transfer_own_key_to_org(self, org_id: str, choice: OwnKeyChoice, *, actor_id: str) -> None:
        """Write a staged registration key into the organization's credential table."""
        self.validate_own_key(choice)
        self.save_credential(org_id, choice.provider, choice.fields, actor_id=actor_id, api_base=choice.api_base)
        if self._store.load_credential(org_id, choice.provider) is None:
            raise AIConfigurationError(
                f'The {self._catalog.provider(choice.provider).label} key was not stored in the organization table.')

    def claim_staged_registration_key(self, org_id: str, actor_id: str) -> bool:
        """Move keys entered before org create into this org if they are still pending."""
        from app.ai import key_staging

        pending = [
            choice for choice in key_staging.load_staged_workflows(actor_id)
            if choice.fields and choice.role in {'analysis', 'redline', 'embedding'}
        ]
        if not pending:
            return False
        dest = key_staging.staged_dest_org_id(actor_id)
        if dest and dest != org_id:
            return False
        try:
            # Apply even when the provider key is already stored. A failed settings
            # write can leave the key behind without the models the user chose.
            self.apply_workflow_choices(org_id, pending, actor_id=actor_id)
            key_staging.discard_staged_key(actor_id)
            return True
        except AIError:
            logger.warning('ai_registration_key_claim_failed', org_id=org_id)
            return False

    async def probe(self, org_id: str, kind: ModelKind, selection: ModelSelection) -> ProbeResult:
        """One tiny call on the org's own credentials. Embeddings report their native width."""
        from app.ai.factory import probe_model

        try:
            spec = self._resolve(kind, selection, require_dimensions=False)
            dimensions = await probe_model(self._resolver.bind(spec, org_id=org_id, byok=True))
        except AIError as exc:
            return ProbeResult(ok=False, detail=str(exc))
        if dimensions is not None and dimensions > self._catalog.storage_dimensions:
            return ProbeResult(ok=False, dimensions=dimensions, detail=(
                f'Model returns {dimensions} dimensions; the maximum is {self._catalog.storage_dimensions}.'))
        return ProbeResult(ok=True, dimensions=dimensions)

    async def verify_credential(
        self,
        org_id: str | None,
        provider: str,
        fields: dict[str, str],
        *,
        api_base: str | None = None,
        model: str | None = None,
        kind: ModelKind | None = None,
    ) -> None:
        """One tiny call with a key that is not stored yet. Raises ``AICredentialInvalid`` if it fails.

        ``org_id`` is None during registration, before the organization exists.
        """
        from app.ai.factory import check_credential

        spec = self._catalog.provider(provider)
        endpoint = validate_api_base(api_base)
        if spec.requires_api_base and not endpoint:
            raise AIConfigurationError(f'{spec.label} needs an endpoint URL (api_base)')
        cleaned, _hint = normalize_credential(provider, fields, keyless=bool(endpoint))
        check_model = self._key_check_model(org_id, provider, model, kind, endpoint)
        target = self._resolver.bind(check_model, org_id=org_id, byok=True, auth=cleaned)
        settings = get_settings()
        settings = settings.model_copy(update={
            'ai_request_timeout_seconds': min(settings.ai_request_timeout_seconds, KEY_CHECK_TIMEOUT_SECONDS),
            'ai_max_retries': 0,
        })
        try:
            await check_credential(target, settings=settings)
        except AIProviderError as exc:
            logger.warning('ai_credential_rejected', org_id=org_id, provider=provider,
                           model=check_model.model, error_type=type(exc).__name__)
            raise AICredentialInvalid(_key_rejected_message(spec.label, exc)) from None
        except AIError as exc:
            logger.warning('ai_credential_rejected', org_id=org_id, provider=provider,
                           model=check_model.model, error_type=type(exc).__name__)
            raise AICredentialInvalid(
                f'{spec.label} could not be verified: {exc} The key was not saved.') from None
        except Exception:
            logger.exception('ai_credential_probe_failed', org_id=org_id, provider=provider,
                             model=check_model.model)
            raise AICredentialInvalid(
                f'{spec.label} did not respond to a test request. The key was not saved.') from None
        logger.info('ai_credential_verified', org_id=org_id, provider=provider, model=check_model.model)

    def _key_check_model(
        self, org_id: str | None, provider: str, model: str | None, kind: ModelKind | None, api_base: str | None,
    ) -> ModelSpec:
        """The model being turned on, else one the org already uses with this provider, else a known one."""
        if model:
            return self._catalog.resolve(provider, model, kind or 'generation', api_base=api_base,
                                         require_dimensions=False)
        known = {(m.model, m.kind): m for m in self._catalog.available_models() if m.provider == provider}
        enabled = next(
            (c.enabled_models for c in self._store.credential_statuses(org_id) if c.provider == provider), ()
        ) if org_id else ()
        kinds: tuple[ModelKind, ...] = ('generation', 'embedding')
        candidates = [known[(name, k)] for name in enabled for k in kinds if (name, k) in known]
        candidates += [m for m in self._catalog.suggested() if m.provider == provider]
        candidates += list(self._catalog.search_models(provider=provider, limit=1).items)
        if not candidates:
            label = self._catalog.provider(provider).label
            raise AIConfigurationError(f'Choose a {label} model so the key can be tested.')
        chosen = candidates[0]
        return self._catalog.resolve(provider, chosen.model, chosen.kind, api_base=api_base,
                                     require_dimensions=False)

    def save_credential(
        self,
        org_id: str,
        provider: str,
        fields: dict[str, str],
        *,
        actor_id: str,
        api_base: str | None = None,
    ) -> CredentialStatus:
        """One key per provider company; every model from that provider uses it."""
        spec = self._catalog.provider(provider)
        endpoint = validate_api_base(api_base)
        if spec.requires_api_base and not endpoint:
            raise AIConfigurationError(f'{spec.label} needs an endpoint URL (api_base)')
        cleaned, hint = normalize_credential(provider, fields, keyless=bool(endpoint))
        return self._store.save_credential(
            org_id, provider, cleaned, hint=hint, actor_id=actor_id, provider_name=spec.label, api_base=endpoint)

    def set_model_enabled(
        self, org_id: str, provider: str, model: str, *, enabled: bool, actor_id: str,
    ) -> CredentialStatus:
        spec = self._catalog.provider(provider)
        self._catalog.validate_model_name(model)
        if not enabled:
            record = self._store.load_settings(org_id)
            in_use = record is not None and record.mode == MODE_BYOK and (provider, model) in {
                (record.generation_provider, record.generation_model),
                (record.analysis_provider, record.analysis_model),
                (record.embedding_provider, record.embedding_model),
            }
            job = self._active_job(org_id)
            in_use = in_use or (job is not None and job.target.mode == MODE_BYOK and (provider, model) in {
                (job.target.generation_provider, job.target.generation_model),
                (job.target.analysis_provider, job.target.analysis_model),
                (job.target.embedding_provider, job.target.embedding_model),
            })
            if in_use:
                raise AIConfigurationError('This model is in use. Choose another model before turning it off.')
        status = self._store.set_model_enabled(org_id, provider, model, enabled=enabled, actor_id=actor_id)
        if status is None:
            raise AICredentialMissing(f'Add your {spec.label} key before turning on its models.')
        return status

    def delete_credential(self, org_id: str, provider: str, *, actor_id: str) -> bool:
        self._catalog.provider(provider)
        record = self._store.load_settings(org_id)
        if record and record.mode == MODE_BYOK and provider in {
            record.generation_provider, record.analysis_provider, record.embedding_provider,
        }:
            raise AIConfigurationError('These credentials are in use. Switch models before removing them.')
        job = self._active_job(org_id)
        if job and job.target.mode == MODE_BYOK and provider in {
            job.target.generation_provider, job.target.analysis_provider, job.target.embedding_provider,
        }:
            raise ReembedInProgress(
                'The model switch in progress uses these credentials. Remove them after it finishes.')
        return self._store.delete_credential(org_id, provider, actor_id=actor_id)
