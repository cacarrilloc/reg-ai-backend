from time import monotonic

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.encryption_posture import encryption_posture
from app.core.observability import (
    langfuse_configured,
    resend_configured,
    sentry_configured,
)
from app.core.security import CurrentUser, get_current_user
from app.db.session import engine, get_db
from app.models.core import DocumentChunk, Policy
from app.schemas.health import AIReadinessResponse, HealthResponse

router = APIRouter(tags=['health'])

# Avoid competing with app traffic for scarce pool slots on every health poll.
_PROBE_CACHE_TTL_SECONDS = 2.0
_probe_cache: tuple[float, tuple[bool, bool | None, str | None]] | None = None


def probe_database(*, force: bool = False) -> tuple[bool, bool | None, str | None]:
    """Return (configured, reachable, error_type) for public health checks."""
    global _probe_cache
    if engine is None:
        return False, None, None

    now = monotonic()
    if not force and _probe_cache is not None:
        cached_at, cached_result = _probe_cache
        if now - cached_at < _PROBE_CACHE_TTL_SECONDS:
            return cached_result

    try:
        with engine.connect() as conn:
            conn.execute(text('select 1'))
        result: tuple[bool, bool | None, str | None] = (True, True, None)
    except SQLAlchemyError as exc:
        result = (True, False, type(exc).__name__)

    _probe_cache = (now, result)
    return result


def build_health_response() -> HealthResponse:
    """Build the deployment health payload used by Railway and operators."""
    settings = get_settings()
    database_configured, database_reachable, database_error = probe_database()
    posture = encryption_posture(settings)
    return HealthResponse(
        status='ok',
        service=settings.app_name,
        environment=settings.environment,
        database_configured=database_configured,
        database_reachable=database_reachable,
        database_error=database_error,
        database_tls_requested=posture['database_tls_requested'],
        transport_https_expected=bool(posture['transport_https_expected']),
        mfa_required_for_all_users=settings.mfa_required_for_all_users,
        mfa_required_for_sensitive_actions=settings.mfa_required_for_sensitive_actions,
        cors_frontend_configured=settings.cors_frontend_configured,
        clerk_configured=settings.clerk_configured,
        supabase_configured=settings.supabase_configured,
        auth_bypass_enabled=settings.auth_bypass_enabled,
        deployment_ready=settings.deployment_ready,
        sentry_configured=sentry_configured(),
        langfuse_configured=langfuse_configured(),
        resend_configured=resend_configured(),
        security_headers_enabled=settings.security_headers_enabled,
        rate_limit_enabled=settings.rate_limit_enabled,
        ip_allowlist_enabled=settings.ip_allowlist_enabled,
    )


@router.get('/health', response_model=HealthResponse)
async def health_check() -> HealthResponse:
    return build_health_response()


@router.get('/health/ai-readiness', response_model=AIReadinessResponse)
async def ai_readiness(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> AIReadinessResponse:
    settings = get_settings()
    anthropic_configured = bool(settings.anthropic_api_key)
    openai_configured = bool(settings.openai_api_key)
    database_configured = db is not None
    database_reachable: bool | None = None
    policy_chunk_count: int | None = None
    embedded_policy_chunk_count: int | None = None
    database_error: str | None = None

    if db is not None:
        try:
            base_query = (
                db.query(DocumentChunk.id)
                .join(Policy, Policy.id == DocumentChunk.policy_id)
                .filter(
                    DocumentChunk.org_id == user.org_id,
                    DocumentChunk.policy_id.is_not(None),
                    Policy.org_id == user.org_id,
                    Policy.status == 'ACTIVE',
                )
            )
            policy_chunk_count = base_query.count()
            embedded_policy_chunk_count = base_query.filter(
                DocumentChunk.embedding.is_not(None)
            ).count()
            database_reachable = True
        except SQLAlchemyError as exc:
            database_reachable = False
            database_error = type(exc).__name__

    semantic_retrieval_ready = bool(
        openai_configured and embedded_policy_chunk_count)

    return AIReadinessResponse(
        status='ok',
        org_id=user.org_id,
        anthropic_configured=anthropic_configured,
        anthropic_opus_model=settings.anthropic_opus_model,
        openai_configured=openai_configured,
        openai_embedding_model=settings.openai_embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        database_configured=database_configured,
        database_reachable=database_reachable,
        policy_chunk_count=policy_chunk_count,
        embedded_policy_chunk_count=embedded_policy_chunk_count,
        llm_gap_analysis_ready=anthropic_configured,
        semantic_retrieval_ready=semantic_retrieval_ready,
        expected_gap_analysis_mode='LLM' if anthropic_configured else 'RULE_FALLBACK',
        expected_retrieval_mode='SEMANTIC_PGVECTOR' if semantic_retrieval_ready else 'LEXICAL_FALLBACK',
        database_error=database_error,
    )
