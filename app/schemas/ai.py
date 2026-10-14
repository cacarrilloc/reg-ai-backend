from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator


class AIModelSelection(BaseModel):
    provider: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=200)
    api_base: str | None = Field(default=None, max_length=500)
    dimensions: int | None = Field(default=None, ge=1)


class AIModeOptionResponse(BaseModel):
    id: Literal['platform', 'byok']
    label: str
    description: str
    billed_to: Literal['reg_ai', 'customer']
    is_default: bool


class AIProviderResponse(BaseModel):
    id: str
    label: str
    requires_api_base: bool


class AIModelResponse(BaseModel):
    provider: str
    model: str
    label: str
    kind: Literal['generation', 'embedding']
    capabilities: list[str]
    dimensions: int | None = None
    is_default: bool = False


class AICatalogResponse(BaseModel):
    modes: list[AIModeOptionResponse]
    providers: list[AIProviderResponse]
    suggested_models: list[AIModelResponse]
    default_generation: AIModelSelection
    default_analysis: AIModelSelection
    default_embedding: AIModelSelection
    max_embedding_dimensions: int
    credential_fields: list[str]
    cloud_credential_fields: dict[str, list[str]]


class AICredentialStatusResponse(BaseModel):
    """A stored provider key, never the secret itself."""

    provider: str
    provider_name: str = ''
    key_hint: str
    api_base: str | None = None
    enabled_models: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None


class AIModelListItem(BaseModel):
    provider: str
    provider_name: str
    model: str
    kind: Literal['generation', 'embedding']
    capabilities: list[str]
    dimensions: int | None = None
    is_default: bool = False
    requires_api_base: bool = False
    credential_fields: list[str]
    # Reg-AI default jobs this model covers: analysis, redline, embedding.
    default_roles: list[Literal['analysis', 'redline', 'embedding']] = Field(default_factory=list)


class AIModelListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[AIModelListItem]


class AIProviderSummaryResponse(BaseModel):
    provider: str
    provider_name: str
    requires_api_base: bool = False
    credential_fields: list[str]
    generation_models: int
    embedding_models: int


class AIProviderListResponse(BaseModel):
    total: int
    items: list[AIProviderSummaryResponse]


class AIModelToggleRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    enabled: bool


class AIMissingCredentialResponse(BaseModel):
    provider: str
    provider_name: str
    purpose: Literal['generation', 'embedding', 'analysis', 'redline']


class AIReembedStatusResponse(BaseModel):
    """An embedding-model switch: documents are re-embedded, then the new model takes over."""

    status: Literal['running', 'swapped', 'completed', 'failed']
    embedding: AIModelSelection
    total_chunks: int
    embedded_chunks: int
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class AISettingsResponse(BaseModel):
    mode: Literal['platform', 'byok']
    mode_label: str
    billed_to: Literal['reg_ai', 'customer']
    generation: AIModelSelection
    analysis: AIModelSelection
    embedding: AIModelSelection
    configured: bool
    credentials: list[AICredentialStatusResponse]
    # Selected models whose provider key is not stored yet (those workflows cannot run).
    missing_credentials: list[AIMissingCredentialResponse] = Field(default_factory=list)
    # Document chunks embedded with ``embedding``. When > 0, another embedding model re-embeds them.
    embedded_chunks: int = 0
    embedding_locked: bool = False
    reembed: AIReembedStatusResponse | None = None
    # Reg-AI default embedding; switching to the defaults re-embeds when it differs from ``embedding``.
    default_embedding: AIModelSelection | None = None
    default_analysis: AIModelSelection | None = None
    default_redline: AIModelSelection | None = None


class AISettingsUpdateRequest(BaseModel):
    mode: Literal['platform', 'byok']
    # Required for ``platform``: the client showed the Reg-AI usage and billing warning.
    accept_reg_ai_billing: bool = False
    # Omitted BYOK selections default to Anthropic (generation) and OpenAI (embeddings).
    generation: AIModelSelection | None = None
    analysis: AIModelSelection | None = None
    embedding: AIModelSelection | None = None
    # Required to change the embedding model once documents are embedded: the user confirmed re-embedding.
    reembed: bool = False


class AICredentialUpdateRequest(BaseModel):
    """``api_key`` for most providers; cloud fields for Bedrock/SageMaker/Vertex.

    ``api_base`` is the endpoint for self-hosted or regional providers; with it the key may be empty.
    """

    fields: dict[str, SecretStr] = Field(default_factory=dict, max_length=12)
    api_base: str | None = Field(default=None, max_length=500)
    # The key is tested with one tiny call on this model before it is stored.
    # Omitted: a model the org already uses with this provider, else a known one.
    model: str | None = Field(default=None, max_length=200)
    kind: Literal['generation', 'embedding'] | None = None


class AICredentialVerifyRequest(BaseModel):
    """Test a key with its provider without storing it (organization registration dialog)."""

    provider: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=200)
    kind: Literal['generation', 'embedding'] = 'generation'
    fields: dict[str, SecretStr] = Field(default_factory=dict, max_length=12)
    api_base: str | None = Field(default=None, max_length=500)


class AICredentialVerifyResponse(BaseModel):
    ok: bool = True


class AIUseDefaultModelsRequest(BaseModel):
    # The client must show the Reg-AI usage and billing warning before sending True.
    accept_reg_ai_billing: bool = False
    # Required when the org's documents are embedded with a non-default model.
    reembed: bool = False


class WorkflowSetup(BaseModel):
    """One job chosen while creating an organization: analysis, redlines, or embeddings."""

    role: Literal['analysis', 'redline', 'embedding']
    provider: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=200)
    fields: dict[str, SecretStr] = Field(default_factory=dict, max_length=12)
    api_base: str | None = Field(default=None, max_length=500)
    dimensions: int | None = Field(default=None, ge=1, le=4096)


class OrganizationAISetup(BaseModel):
    """Mandatory when creating an organization: Reg-AI default models or the org's own keys."""

    option: Literal['reg_ai_default', 'own_key']
    accept_reg_ai_billing: bool = False
    # Analysis and redlines, chosen separately. A job left out stays on the Reg-AI default.
    workflows: list[WorkflowSetup] = Field(default_factory=list)

    @model_validator(mode='after')
    def _choice_is_complete(self) -> OrganizationAISetup:
        if self.option == 'reg_ai_default' and not self.accept_reg_ai_billing:
            raise ValueError('Accept Reg-AI usage billing to use the Reg-AI default models')
        if self.option == 'own_key' and not self.workflows:
            raise ValueError(
                'Choose a model for analysis, redlines, or embeddings, and enter its provider key')
        return self


class AIModelTestRequest(BaseModel):
    kind: Literal['generation', 'embedding']
    selection: AIModelSelection


class AIModelTestResponse(BaseModel):
    ok: bool
    dimensions: int | None = None
    detail: str | None = None
