import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import ClientDisconnect

from app.api.v1.endpoints.health import build_health_response
from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.inngest import register_inngest
from app.core.langfuse_tracing import LangfuseTracingMiddleware
from app.core.logging import configure_logging
from app.core.observability import (
    configure_observability,
    langfuse_configured,
    resend_configured,
    sentry_configured,
    shutdown_observability,
)
from app.core.security import ClerkAuthMiddleware
from app.core.security_hardening import (
    InboundRateLimitMiddleware,
    IPAllowlistMiddleware,
    SecurityHeadersMiddleware,
)
from app.schemas.health import HealthResponse
from app.services.storage_service import ensure_configured_storage_buckets

configure_logging()
configure_observability()
settings = get_settings()

# FastAPI calls request.form() with Starlette's 1 MB default for non-file parts.
# Pitch-deck metadata is small, but keep the parser aligned with MAX_POLICY_UPLOAD_MB.
_ORIGINAL_REQUEST_FORM = Request.form
_MAX_UPLOAD_PART_BYTES = settings.max_policy_upload_mb * 1024 * 1024


def _request_form_with_upload_limit(
    self: Request,
    *,
    max_files: int | float = 1000,
    max_fields: int | float = 1000,
    max_part_size: int = _MAX_UPLOAD_PART_BYTES,
):
    return _ORIGINAL_REQUEST_FORM(
        self,
        max_files=max_files,
        max_fields=max_fields,
        max_part_size=max_part_size,
    )


setattr(Request, 'form', _request_form_with_upload_limit)

app = FastAPI(
    title=settings.app_name,
    version='0.1.0',
    description='Reg-AI Policy Change Agent FastAPI service scaffold.',
)

# Last added = outermost. Request order: CORS → headers → IP → rate limit → auth → tracing.
app.add_middleware(LangfuseTracingMiddleware)
app.add_middleware(ClerkAuthMiddleware)
app.add_middleware(InboundRateLimitMiddleware)
app.add_middleware(IPAllowlistMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
    # Local/LAN frontends (Chrome PNA) send Access-Control-Request-Private-Network.
    # Starlette 1.x rejects those preflights unless explicitly allowed.
    allow_private_network=not settings.is_production,
)
app.include_router(api_router, prefix=settings.api_v1_prefix)
register_inngest(app)


@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(_request: Request, _exc: ClientDisconnect) -> Response:
    """Clients often disconnect while long background work is running; avoid noisy tracebacks."""
    structlog.get_logger(__name__).debug('client_disconnect')
    return Response(status_code=499)


@app.on_event('startup')
def bootstrap_storage_buckets() -> None:
    logger = structlog.get_logger(__name__)
    if settings.is_railway and not settings.cors_frontend_configured:
        logger.error(
            'cors_misconfigured_for_railway',
            hint='Set CORS_ORIGINS, FRONTEND_URL, or CLERK_AUTHORIZED_PARTIES to your Vercel URL.',
        )
    elif settings.is_production and not settings.cors_frontend_configured:
        logger.error(
            'cors_misconfigured_for_production',
            hint='Set CORS_ORIGINS and/or FRONTEND_URL to your Vercel URL on Railway.',
        )
    if settings.is_railway and settings.is_production and settings.auth_bypass_enabled:
        logger.error(
            'auth_bypass_enabled_in_production',
            hint='Set AUTH_BYPASS_ENABLED=false on Railway.',
        )
    if settings.is_railway and not settings.database_url:
        logger.error(
            'database_url_missing_on_railway',
            hint='Set DATABASE_URL to your Supabase pooler connection string.',
        )
    observability = {
        'sentry': sentry_configured(),
        'langfuse': langfuse_configured(),
        'resend': resend_configured(),
    }
    if settings.is_production and not all(observability.values()):
        logger.info('observability_status', **observability)
    try:
        ensure_configured_storage_buckets(settings)
    except Exception as exc:
        logger.warning(
            'storage_bucket_bootstrap_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )

    if not settings.database_url:
        return

    try:
        from app.db.session import SessionLocal
        from app.services.internal_org_service import seed_internal_environment_organization

        if SessionLocal is not None:
            with SessionLocal() as db:
                seed_internal_environment_organization(db, settings=settings)
    except Exception as exc:
        logger.warning(
            'internal_org_seed_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:500],
            hint='Apply alembic upgrade head (phase_26) then restart.',
        )

    try:
        from sqlalchemy import text

        from app.db.session import engine

        if engine is None:
            return
        with engine.connect() as conn:
            document_type_column_exists = conn.execute(
                text(
                    'select exists ('
                    'select 1 from information_schema.columns '
                    "where table_schema = 'public' "
                    "and table_name = 'document_chunks' "
                    "and column_name = 'document_type'"
                    ')'
                )
            ).scalar()
            policy_name_column_exists = conn.execute(
                text(
                    'select exists ('
                    'select 1 from information_schema.columns '
                    "where table_schema = 'public' "
                    "and table_name = 'document_chunks' "
                    "and column_name = 'policy_name'"
                    ')'
                )
            ).scalar()
        if not document_type_column_exists:
            logger.error(
                'database_schema_outdated',
                missing='document_chunks.document_type',
                fix='Run: alembic upgrade head',
            )
            return
        if not policy_name_column_exists:
            logger.error(
                'database_schema_outdated',
                missing='document_chunks.policy_name',
                fix='Run: alembic upgrade head',
            )
    except Exception as exc:
        logger.warning(
            'database_schema_check_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )


@app.on_event('shutdown')
def shutdown_observability_handlers() -> None:
    try:
        from app.db.session import dispose_engines

        dispose_engines()
    except Exception:
        pass
    shutdown_observability()


@app.middleware('http')
async def allow_local_private_network_access(request: Request, call_next):
    '''Allow access to local private network when the request has the appropriate header.'''
    response = await call_next(request)
    if (
        not settings.is_production
        and request.headers.get('access-control-request-private-network', '').lower() == 'true'
    ):
        response.headers['Access-Control-Allow-Private-Network'] = 'true'
    return response


@app.get('/')
async def root() -> dict[str, str]:
    return {'service': settings.app_name, 'docs': '/docs', 'health': '/health'}


@app.get('/health', response_model=HealthResponse, tags=['health'])
async def top_level_health_check() -> HealthResponse:
    return build_health_response()


if sentry_configured() and not settings.is_production:

    @app.get('/sentry-debug', include_in_schema=False, tags=['health'])
    async def sentry_debug() -> None:
        """Trigger a test error for Sentry verification (local/staging only)."""
        raise ZeroDivisionError('Sentry debug endpoint')


@app.get('/favicon.ico', include_in_schema=False)
async def favicon() -> Response:
    return Response(status_code=status.HTTP_204_NO_CONTENT)
