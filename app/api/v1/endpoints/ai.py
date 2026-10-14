from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool

from app.ai.catalog import get_model_catalog
from app.ai.credentials import CLOUD_AUTH_FIELDS, CREDENTIAL_FIELDS
from app.ai.reembedding import ReembedInProgress, ReembedJob, dispatch_reembed_job
from app.ai.settings_service import (
    MODE_LABELS,
    MODE_OPTIONS,
    EmbeddingProfileLocked,
    ModelSelection,
    OrgAISettingsService,
    OrgAIView,
)
from app.ai.store import CredentialStatus
from app.ai.types import AIConfigurationError, AICredentialMissing, AIEncryptionUnavailable, AIError
from app.core.roles import ORG_MANAGEMENT_ROLES
from app.core.security import CurrentUser, get_current_user, require_aal2
from app.schemas.ai import (
    AICatalogResponse,
    AICredentialStatusResponse,
    AICredentialUpdateRequest,
    AICredentialVerifyRequest,
    AICredentialVerifyResponse,
    AIMissingCredentialResponse,
    AIModelListItem,
    AIModelListResponse,
    AIModelResponse,
    AIModelSelection,
    AIModelTestRequest,
    AIModelTestResponse,
    AIModelToggleRequest,
    AIModeOptionResponse,
    AIProviderListResponse,
    AIProviderResponse,
    AIProviderSummaryResponse,
    AIReembedStatusResponse,
    AISettingsResponse,
    AISettingsUpdateRequest,
    AIUseDefaultModelsRequest,
)

router = APIRouter(prefix='/ai', tags=['ai'])


def require_ai_settings_manager(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.has_any_role(ORG_MANAGEMENT_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail='Only Master and CCO users can manage AI settings')
    return user


def require_ai_settings_writer(
    user: CurrentUser = Depends(require_ai_settings_manager),
    _mfa: CurrentUser = Depends(require_aal2),
) -> CurrentUser:
    return user


def _http_error(exc: AIError) -> HTTPException:
    if isinstance(exc, AIEncryptionUnavailable):
        return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    if isinstance(exc, EmbeddingProfileLocked | AICredentialMissing | ReembedInProgress):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, AIConfigurationError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


def _to_schema(selection: ModelSelection) -> AIModelSelection:
    return AIModelSelection(provider=selection.provider, model=selection.model,
                            api_base=selection.api_base, dimensions=selection.dimensions)


def _from_schema(value: AIModelSelection | None) -> ModelSelection | None:
    return ModelSelection(value.provider, value.model, value.api_base, value.dimensions) if value else None


def _reembed_response(job: ReembedJob | None) -> AIReembedStatusResponse | None:
    if job is None:
        return None
    target = job.target
    return AIReembedStatusResponse(
        status=job.status,  # type: ignore[arg-type]
        embedding=AIModelSelection(provider=target.embedding_provider, model=target.embedding_model,
                                   api_base=target.embedding_api_base, dimensions=target.embedding_dimensions),
        total_chunks=job.total_chunks,
        embedded_chunks=job.embedded_chunks,
        error=job.error,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _settings_service(background_tasks: BackgroundTasks) -> OrgAISettingsService:
    return OrgAISettingsService(
        dispatch_reembed=lambda job_id: background_tasks.add_task(dispatch_reembed_job, job_id))


def _settings_response(view: OrgAIView) -> AISettingsResponse:
    return AISettingsResponse(
        mode=view.mode,  # type: ignore[arg-type]
        mode_label=MODE_LABELS[view.mode],
        billed_to='customer' if view.mode == 'byok' else 'reg_ai',
        generation=_to_schema(view.generation),
        analysis=_to_schema(view.analysis),
        embedding=_to_schema(view.embedding),
        configured=view.configured,
        credentials=[_credential_response(c) for c in view.credentials],
        missing_credentials=[
            AIMissingCredentialResponse(provider=m.provider, provider_name=m.provider_name, purpose=m.purpose)
            for m in view.missing_credentials
        ],
        embedded_chunks=view.embedded_chunks,
        embedding_locked=view.embedding_locked,
        reembed=_reembed_response(view.reembed),
        default_embedding=_default_embedding(),
        default_analysis=_default_model('analysis'),
        default_redline=_default_model('redline'),
    )


def _default_model(purpose: Literal['analysis', 'redline', 'embedding']) -> AIModelSelection:
    spec = get_model_catalog().platform_default(purpose)
    return AIModelSelection(provider=spec.provider, model=spec.model, api_base=spec.api_base,
                            dimensions=spec.dimensions)


def _default_embedding() -> AIModelSelection:
    return _default_model('embedding')


def _credential_response(status: CredentialStatus) -> AICredentialStatusResponse:
    return AICredentialStatusResponse(
        provider=status.provider,
        provider_name=status.provider_name,
        key_hint=status.key_hint,
        api_base=status.api_base,
        enabled_models=list(status.enabled_models),
        updated_at=status.updated_at,
    )


@router.get('/models', response_model=AIModelListResponse)
def list_ai_models(
    q: str = Query(default='', max_length=200),
    kind: Literal['generation', 'embedding'] | None = None,
    provider: str | None = Query(default=None, max_length=60),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _user: CurrentUser = Depends(get_current_user),
) -> AIModelListResponse:
    """Every chat and embedding model LiteLLM supports, searchable, Reg-AI defaults first."""
    catalog = get_model_catalog()
    page = catalog.search_models(q, kind=kind, provider=provider, offset=offset, limit=limit)
    items = []
    for m in page.items:
        spec = catalog.provider(m.provider)
        items.append(AIModelListItem(
            provider=m.provider, provider_name=spec.label, model=m.model, kind=m.kind,
            capabilities=sorted(m.capabilities), dimensions=m.dimensions, is_default=m.is_default,
            requires_api_base=spec.requires_api_base,
            credential_fields=sorted(CLOUD_AUTH_FIELDS.get(m.provider, frozenset({'api_key'}))),
            default_roles=list(catalog.default_roles(m.provider, m.model, m.kind)),
        ))
    return AIModelListResponse(total=page.total, offset=offset, limit=limit, items=items)


@router.get('/providers', response_model=AIProviderListResponse)
def list_ai_providers(
    q: str = Query(default='', max_length=200),
    kind: Literal['generation', 'embedding'] | None = None,
    _user: CurrentUser = Depends(get_current_user),
) -> AIProviderListResponse:
    """Providers with at least one model matching ``q``, alphabetical. Each one groups its models."""
    summaries = get_model_catalog().provider_summaries(q, kind=kind)
    items = [
        AIProviderSummaryResponse(
            provider=s.provider.id, provider_name=s.provider.label, requires_api_base=s.provider.requires_api_base,
            credential_fields=sorted(CLOUD_AUTH_FIELDS.get(s.provider.id, frozenset({'api_key'}))),
            generation_models=s.generation_models, embedding_models=s.embedding_models,
        )
        for s in summaries
    ]
    return AIProviderListResponse(total=len(items), items=items)


@router.get('/catalog', response_model=AICatalogResponse)
def get_ai_catalog(_user: CurrentUser = Depends(get_current_user)) -> AICatalogResponse:
    """Every allowed provider, suggested models, and the Reg-AI defaults for the setup page."""
    catalog = get_model_catalog()
    generation = catalog.platform_default('redline')
    analysis = catalog.platform_default('analysis')
    embedding = catalog.platform_default('embedding')
    return AICatalogResponse(
        modes=[
            AIModeOptionResponse(
                id=o.id, label=o.label, description=o.description,  # type: ignore[arg-type]
                billed_to=o.billed_to, is_default=o.is_default,  # type: ignore[arg-type]
            )
            for o in MODE_OPTIONS
        ],
        providers=[
            AIProviderResponse(id=p.id, label=p.label, requires_api_base=p.requires_api_base)
            for p in catalog.providers()
        ],
        suggested_models=[
            AIModelResponse(
                provider=m.provider, model=m.model, label=m.label, kind=m.kind,
                capabilities=sorted(m.capabilities), dimensions=m.dimensions, is_default=m.is_default,
            )
            for m in catalog.suggested()
        ],
        default_generation=AIModelSelection(provider=generation.provider, model=generation.model),
        default_analysis=AIModelSelection(provider=analysis.provider, model=analysis.model),
        default_embedding=AIModelSelection(
            provider=embedding.provider, model=embedding.model, dimensions=embedding.dimensions),
        max_embedding_dimensions=catalog.storage_dimensions,
        credential_fields=sorted(CREDENTIAL_FIELDS),
        cloud_credential_fields={p: sorted(f) for p, f in sorted(CLOUD_AUTH_FIELDS.items())},
    )


@router.get('/settings', response_model=AISettingsResponse)
def get_ai_settings(user: CurrentUser = Depends(require_ai_settings_manager)) -> AISettingsResponse:
    """Current org selection and which providers have stored credentials (hints only)."""
    try:
        service = OrgAISettingsService()
        service.claim_staged_registration_key(user.org_id, user.user_id)
        return _settings_response(service.view(user.org_id))
    except AIError as exc:
        raise _http_error(exc) from None


@router.put('/settings', response_model=AISettingsResponse)
def put_ai_settings(
    payload: AISettingsUpdateRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_ai_settings_writer),
) -> AISettingsResponse:
    """Save the org's models. A new embedding model on embedded documents needs ``reembed``;
    the response then reports the background re-embed and the old model stays until it finishes."""
    try:
        view = _settings_service(background_tasks).update(
            user.org_id,
            mode=payload.mode,
            generation=_from_schema(payload.generation),
            analysis=_from_schema(payload.analysis),
            embedding=_from_schema(payload.embedding),
            actor_id=user.user_id,
            accept_billing=payload.accept_reg_ai_billing,
            reembed=payload.reembed,
        )
    except AIError as exc:
        raise _http_error(exc) from None
    return _settings_response(view)


@router.post('/settings/use-default-models', response_model=AISettingsResponse)
def post_use_default_models(
    payload: AIUseDefaultModelsRequest,
    background_tasks: BackgroundTasks,
    user: CurrentUser = Depends(require_ai_settings_writer),
) -> AISettingsResponse:
    """Select "Use Reg-AI default models": Reg-AI's models on Reg-AI keys, billed by Reg-AI.

    Requires ``accept_reg_ai_billing``: the client showed the usage and billing warning.
    """
    try:
        view = _settings_service(background_tasks).use_default_models(
            user.org_id, actor_id=user.user_id, accept_billing=payload.accept_reg_ai_billing,
            reembed=payload.reembed)
    except AIError as exc:
        raise _http_error(exc) from None
    return _settings_response(view)


@router.post('/test', response_model=AIModelTestResponse)
async def post_ai_model_test(
    payload: AIModelTestRequest,
    user: CurrentUser = Depends(require_ai_settings_writer),
) -> AIModelTestResponse:
    """One tiny call billed to the org's credentials; reports embedding width."""
    selection = _from_schema(payload.selection)
    assert selection is not None
    result = await OrgAISettingsService().probe(user.org_id, payload.kind, selection)
    return AIModelTestResponse(ok=result.ok, dimensions=result.dimensions, detail=result.detail)


@router.post('/credentials/verify', response_model=AICredentialVerifyResponse)
async def post_ai_credential_verify(
    payload: AICredentialVerifyRequest,
    _user: CurrentUser = Depends(get_current_user),
) -> AICredentialVerifyResponse:
    """One tiny call with a key that is not stored. Used in the organization-creation model dialog.

    A 401/403 from the provider is a failed verification. The key is never saved here.
    """
    fields = {name: value.get_secret_value() for name, value in payload.fields.items()}
    try:
        await OrgAISettingsService().verify_credential(
            None, payload.provider, fields, api_base=payload.api_base, model=payload.model, kind=payload.kind)
    except AIError as exc:
        raise _http_error(exc) from None
    return AICredentialVerifyResponse(ok=True)


@router.put('/credentials/{provider}', response_model=AICredentialStatusResponse)
async def put_ai_credential(
    provider: str,
    payload: AICredentialUpdateRequest,
    user: CurrentUser = Depends(require_ai_settings_writer),
) -> AICredentialStatusResponse:
    """Test the org's key for one provider company, then store it encrypted in the org's tenant schema.

    A key the provider does not accept is never stored. Secrets are never returned.
    Every model from this provider uses this one key.
    """
    fields = {name: value.get_secret_value() for name, value in payload.fields.items()}
    service = OrgAISettingsService()
    try:
        await service.verify_credential(
            user.org_id, provider, fields, api_base=payload.api_base, model=payload.model, kind=payload.kind)
        saved = await run_in_threadpool(
            service.save_credential, user.org_id, provider, fields, actor_id=user.user_id, api_base=payload.api_base)
    except AIError as exc:
        raise _http_error(exc) from None
    return _credential_response(saved)


@router.put('/credentials/{provider}/models', response_model=AICredentialStatusResponse)
def put_ai_provider_model(
    provider: str,
    payload: AIModelToggleRequest,
    user: CurrentUser = Depends(require_ai_settings_writer),
) -> AICredentialStatusResponse:
    """Turn one model on or off under the provider key the org already stored."""
    try:
        saved = OrgAISettingsService().set_model_enabled(
            user.org_id, provider, payload.model, enabled=payload.enabled, actor_id=user.user_id)
    except AIError as exc:
        raise _http_error(exc) from None
    return _credential_response(saved)


@router.delete('/credentials/{provider}', status_code=status.HTTP_204_NO_CONTENT)
def delete_ai_credential(provider: str, user: CurrentUser = Depends(require_ai_settings_writer)) -> None:
    try:
        deleted = OrgAISettingsService().delete_credential(user.org_id, provider, actor_id=user.user_id)
    except AIError as exc:
        raise _http_error(exc) from None
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='No stored credentials for this provider')
