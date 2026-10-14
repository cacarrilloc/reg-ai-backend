from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    database_configured: bool = False
    database_reachable: bool | None = None
    database_error: str | None = None
    database_tls_requested: bool | None = None
    transport_https_expected: bool = False
    mfa_required_for_all_users: bool = True
    mfa_required_for_sensitive_actions: bool = True
    cors_frontend_configured: bool = False
    clerk_configured: bool = False
    supabase_configured: bool = False
    auth_bypass_enabled: bool = True
    deployment_ready: bool = False
    sentry_configured: bool = False
    langfuse_configured: bool = False
    resend_configured: bool = False
    security_headers_enabled: bool = True
    rate_limit_enabled: bool = True
    ip_allowlist_enabled: bool = False


class AIReadinessResponse(BaseModel):
    status: str
    org_id: str
    anthropic_configured: bool
    anthropic_opus_model: str
    openai_configured: bool
    openai_embedding_model: str
    embedding_dimensions: int
    database_configured: bool
    database_reachable: bool | None = None
    policy_chunk_count: int | None = None
    embedded_policy_chunk_count: int | None = None
    llm_gap_analysis_ready: bool
    semantic_retrieval_ready: bool
    expected_gap_analysis_mode: str
    expected_retrieval_mode: str
    database_error: str | None = None
