import os
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})
PUBLIC_FRONTEND_URL_PRODUCTION = "https://reg-ai-production.vercel.app"
PUBLIC_FRONTEND_URL_STAGING = "https://reg-ai-staging.vercel.app"
PUBLIC_FRONTEND_URL_DEV = "https://reg-ai-dev.vercel.app"


def is_public_frontend_origin(origin: str) -> bool:
    """True for an https origin that recipients can open from an email client."""
    raw = (origin or "").strip().rstrip("/")
    if not raw.startswith("https://"):
        return False
    try:
        host = (urlparse(raw).hostname or "").lower()
    except ValueError:
        return False
    if not host or host in _LOOPBACK_HOSTS or host.endswith(".local"):
        return False
    if host.startswith("10.") or host.startswith("192.168."):
        return False
    if host.startswith("172."):
        second = host.split(".")[1] if "." in host else ""
        if second.isdigit() and 16 <= int(second) <= 31:
            return False
    return True


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file.

    Uses pydantic-settings BaseSettings to provide typed configuration values
    for the Reg-AI backend service.
    """

    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore")

    app_name: str = Field(default="Reg-AI FastAPI Service", alias="APP_NAME")
    environment: str = Field(default="local", alias="ENVIRONMENT")
    api_v1_prefix: str = Field(default="/api/v1", alias="API_V1_PREFIX")
    cors_origins: str = Field(
        default="http://localhost:3000", alias="CORS_ORIGINS")

    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    direct_url: str | None = Field(
        default=None,
        alias="DIRECT_URL",
        description="Optional non-pooler URL for Alembic / migrations.",
    )
    database_pool_size: int = Field(default=8, alias="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(default=4, alias="DATABASE_MAX_OVERFLOW")
    database_pool_timeout: int = Field(default=30, alias="DATABASE_POOL_TIMEOUT")
    database_pool_recycle: int = Field(default=600, alias="DATABASE_POOL_RECYCLE")
    database_connect_timeout: int = Field(default=10, alias="DATABASE_CONNECT_TIMEOUT")
    database_background_task_limit: int = Field(
        default=2, alias="DATABASE_BACKGROUND_TASK_LIMIT")

    # Supabase Auth (control-plane IdP). Prefer SUPABASE_JWKS_URL / SUPABASE_JWT_ISSUER.
    supabase_jwks_url: str | None = Field(default=None, alias="SUPABASE_JWKS_URL")
    supabase_jwt_issuer: str | None = Field(default=None, alias="SUPABASE_JWT_ISSUER")
    supabase_jwt_audience: str | None = Field(
        default="authenticated", alias="SUPABASE_JWT_AUDIENCE")
    auth_org_claim: str = Field(default="org_id", alias="AUTH_ORG_CLAIM")
    auth_role_claim: str = Field(default="app_metadata.role", alias="AUTH_ROLE_CLAIM")
    auth_bypass_enabled: bool = Field(
        default=True, alias="AUTH_BYPASS_ENABLED")
    mfa_required_for_sensitive_actions: bool = Field(
        default=True, alias="MFA_REQUIRED_FOR_SENSITIVE_ACTIONS")
    mfa_required_for_all_users: bool = Field(
        default=True,
        alias="MFA_REQUIRED_FOR_ALL_USERS",
        description="Product-wide MFA enrollment expectation (paired with frontend gate).",
    )
    sso_enabled: bool = Field(default=True, alias="SSO_ENABLED")

    # Deprecated Clerk env aliases — still read so old deploy files fail soft during cutover.
    clerk_issuer: str | None = Field(default=None, alias="CLERK_ISSUER")
    clerk_jwks_url: str | None = Field(default=None, alias="CLERK_JWKS_URL")
    clerk_audience: str | None = Field(default=None, alias="CLERK_AUDIENCE")
    clerk_authorized_parties: str = Field(
        default="", alias="CLERK_AUTHORIZED_PARTIES")
    clerk_org_claim: str = Field(default="org_id", alias="CLERK_ORG_CLAIM")
    clerk_role_claim: str = Field(
        default="app_metadata.role", alias="CLERK_ROLE_CLAIM")
    clerk_secret_key: str | None = Field(
        default=None, alias="CLERK_SECRET_KEY")

    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_access_token: str | None = Field(
        default=None, alias="SUPABASE_ACCESS_TOKEN")
    supabase_service_role_key: str | None = Field(
        default=None, alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_storage_bucket: str = Field(
        default="policy-documents", alias="SUPABASE_STORAGE_BUCKET")
    supabase_storage_buckets: str = Field(
        default="policy-documents,policy-redlines,approved-policies,context-documents,compliance-reports,business-change-analyses,marketing-contents,organization-logos",
        alias="SUPABASE_STORAGE_BUCKETS",
    )
    # Management API: create one Supabase project per organization.
    supabase_platform_org_slug: str | None = Field(
        default=None, alias="SUPABASE_PLATFORM_ORG_SLUG")
    supabase_tenant_region: str = Field(
        default="us-east-1", alias="SUPABASE_TENANT_REGION")
    supabase_tenant_db_instance_size: str = Field(
        default="micro", alias="SUPABASE_TENANT_DB_INSTANCE_SIZE")
    tenant_provisioning_enabled: bool = Field(
        default=True, alias="TENANT_PROVISIONING_ENABLED")
    tenant_isolation_mode: str = Field(
        default="schema",
        alias="TENANT_ISOLATION_MODE",
        description="schema = Postgres schema per org on this project; project = legacy Management API.",
    )
    tenant_db_routing_enabled: bool = Field(
        default=True, alias="TENANT_DB_ROUTING_ENABLED")
    # Comma-separated Supabase Auth user ids joined to this env's internal org on seed.
    internal_org_member_user_ids: str = Field(
        default="", alias="INTERNAL_ORG_MEMBER_USER_IDS")
    # Platform master / super-admin email (env internal org bootstrap + join).
    master_account: str = Field(
        default="haider.mumtaz@ipxenterprises.com",
        alias="MASTER_ACCOUNT",
    )
    frontend_url: str = Field(
        default="http://localhost:3000", alias="FRONTEND_URL")

    stripe_secret_key: str | None = Field(default=None, alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str | None = Field(
        default=None, alias="STRIPE_WEBHOOK_SECRET")
    stripe_price_monthly: str | None = Field(
        default=None, alias="STRIPE_PRICE_MONTHLY")
    stripe_price_annual: str | None = Field(
        default=None, alias="STRIPE_PRICE_ANNUAL")
    support_notify_email: str = Field(
        default="haider.mumtaz@ipxenterprises.com",
        alias="SUPPORT_NOTIFY_EMAIL",
    )
    crisp_website_id: str | None = Field(default=None, alias="CRISP_WEBSITE_ID")

    anthropic_api_key: str | None = Field(
        default=None, alias="ANTHROPIC_API_KEY")
    # Judgment-heavy paths: gap analysis, business-change assessment, compliance reports.
    anthropic_opus_model: str = Field(
        default="claude-opus-4-6", alias="ANTHROPIC_OPUS_MODEL")
    # Volume drafting path: tracked-change redlines (faster/cheaper than Opus).
    anthropic_sonnet_model: str = Field(
        default="claude-sonnet-4-6", alias="ANTHROPIC_SONNET_MODEL")
    anthropic_gap_analysis_model: str | None = Field(
        default=None, alias="ANTHROPIC_GAP_ANALYSIS_MODEL")
    anthropic_redline_model: str | None = Field(
        default=None, alias="ANTHROPIC_REDLINE_MODEL")
    anthropic_input_token_budget_per_minute: int = Field(
        default=9_000, alias="ANTHROPIC_INPUT_TOKEN_BUDGET_PER_MINUTE")
    anthropic_chars_per_token: int = Field(
        default=4, alias="ANTHROPIC_CHARS_PER_TOKEN")
    business_change_assessment_max_tokens: int = Field(
        default=350, alias="BUSINESS_CHANGE_ASSESSMENT_MAX_TOKENS")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_embedding_model: str = Field(
        default="text-embedding-3-large", alias="OPENAI_EMBEDDING_MODEL")
    embedding_dimensions: int = Field(
        default=3072, alias="EMBEDDING_DIMENSIONS")
    openai_requests_per_second: float = Field(
        default=10.0, alias="OPENAI_REQUESTS_PER_SECOND")
    anthropic_opus_min_interval_seconds: float = Field(
        default=20.0, alias="ANTHROPIC_OPUS_MIN_INTERVAL_SECONDS")
    anthropic_sonnet_min_interval_seconds: float = Field(
        default=2.0, alias="ANTHROPIC_SONNET_MIN_INTERVAL_SECONDS")
    anthropic_rate_limit_retry_seconds: float = Field(
        default=65.0, alias="ANTHROPIC_RATE_LIMIT_RETRY_SECONDS")
    anthropic_retry_delay_seconds: float = Field(
        default=2.0, alias="ANTHROPIC_RETRY_DELAY_SECONDS")
    anthropic_rate_limit_max_attempts: int = Field(
        default=6, alias="ANTHROPIC_RATE_LIMIT_MAX_ATTEMPTS")
    # BYOK model layer (app/ai). Comma-separated base64 32-byte keys; the first one encrypts.
    ai_credential_encryption_keys: str | None = Field(
        default=None, alias="AI_CREDENTIAL_ENCRYPTION_KEYS")
    ai_request_timeout_seconds: float = Field(
        default=600.0, alias="AI_REQUEST_TIMEOUT_SECONDS")
    ai_max_retries: int = Field(default=2, alias="AI_MAX_RETRIES")
    ai_requests_per_second: float = Field(
        default=2.0, alias="AI_REQUESTS_PER_SECOND")
    # Non-production only: allow http and private hosts for custom model endpoints.
    ai_allow_private_api_base: bool = Field(
        default=False, alias="AI_ALLOW_PRIVATE_API_BASE")
    # Encrypted registration keys live here until the org schema can store them.
    ai_key_staging_dir: str | None = Field(default=None, alias="AI_KEY_STAGING_DIR")

    inngest_app_id: str = Field(
        default="reg-ai-backend", alias="INNGEST_APP_ID")
    inngest_event_key: str | None = Field(
        default=None, alias="INNGEST_EVENT_KEY")
    inngest_signing_key: str | None = Field(
        default=None, alias="INNGEST_SIGNING_KEY")
    resend_api_key: str | None = Field(default=None, alias="RESEND_API_KEY")
    resend_from_email: str = Field(
        default="Reg-AI <notifications@alerts.ipxenterprises.com>",
        alias="RESEND_FROM_EMAIL",
    )
    langfuse_public_key: str | None = Field(
        default=None, alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str | None = Field(
        default=None, alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str | None = Field(
        default="https://cloud.langfuse.com",
        alias="LANGFUSE_HOST",
    )
    sentry_dsn: str | None = Field(default=None, alias="SENTRY_DSN")
    sentry_traces_sample_rate: float = Field(
        default=0.2, alias="SENTRY_TRACES_SAMPLE_RATE")
    sentry_profiles_sample_rate: float = Field(
        default=0.0, alias="SENTRY_PROFILES_SAMPLE_RATE")
    sentry_send_default_pii: bool = Field(
        default=False, alias="SENTRY_SEND_DEFAULT_PII")
    # Comma-separated exact IPs allowed to reach the API. Empty = allow all.
    allowed_ips: str = Field(default="", alias="ALLOWED_IPS")
    # Comma-separated CIDR ranges (e.g. 10.0.0.0/8,192.168.0.0/16). Empty = none.
    allowed_ip_cidrs: str = Field(default="", alias="ALLOWED_IP_CIDRS")
    security_headers_enabled: bool = Field(
        default=True, alias="SECURITY_HEADERS_ENABLED")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")
    rate_limit_requests_per_minute: int = Field(
        default=120, alias="RATE_LIMIT_REQUESTS_PER_MINUTE")
    rate_limit_auth_failures_per_minute: int = Field(
        default=30, alias="RATE_LIMIT_AUTH_FAILURES_PER_MINUTE")
    clerk_webhook_secret: str | None = Field(
        default=None, alias="CLERK_WEBHOOK_SECRET")
    supabase_webhook_secret: str | None = Field(
        default=None, alias="SUPABASE_WEBHOOK_SECRET")
    sec_user_agent: str = Field(
        default="Reg-AI compliance-monitoring haider.mumtaz@ipxenterprises.com", alias="SEC_USER_AGENT")
    sec_newsroom_urls: str = Field(
        default=(
            # Official newsroom hub (HTML) plus RSS backups when the hub returns 503.
            "https://www.sec.gov/newsroom,"
            "https://www.sec.gov/news/pressreleases.rss,"
            "https://www.sec.gov/news/speeches-statements.rss"
        ),
        alias="SEC_NEWSROOM_URLS",
    )
    finra_rss_urls: str = Field(
        default=(
            # Official media-center hub (HTML) plus RSS backups (HTTP; HTTPS TLS often fails).
            "https://www.finra.org/media-center,"
            "http://feeds.finra.org/FINRANotices,"
            "http://feeds.finra.org/FINRANews"
        ),
        alias="FINRA_RSS_URLS",
    )
    cftc_rss_urls: str = Field(
        default=(
            # Official Press Room hub (HTML) plus press-release RSS backup.
            "https://www.cftc.gov/PressRoom/index.htm,"
            "https://www.cftc.gov/RSS/RSSGP/rssgp.xml"
        ),
        alias="CFTC_RSS_URLS",
    )
    nfa_rss_urls: str = Field(default="", alias="NFA_RSS_URLS")
    regulatory_max_items_per_feed: int = Field(
        default=25, alias="REGULATORY_MAX_ITEMS_PER_FEED")
    # Keep only the newest N REGULATORY_REFERENCE policy versions per org/source.
    regulatory_reference_max_versions: int = Field(
        default=10, alias="REGULATORY_REFERENCE_MAX_VERSIONS")
    regulatory_request_timeout_seconds: float = Field(
        default=30.0, alias="REGULATORY_REQUEST_TIMEOUT_SECONDS")
    regulatory_http_max_attempts: int = Field(
        default=5, alias="REGULATORY_HTTP_MAX_ATTEMPTS")
    # Prefer RSS/listing summaries by default. Full HTML article fetches are rate-limited
    # heavily by FINRA (429) and SEC newsroom pages (503).
    regulatory_fetch_full_text: bool = Field(
        default=False, alias="REGULATORY_FETCH_FULL_TEXT")
    finra_requests_per_second: float = Field(
        default=0.05, alias="FINRA_REQUESTS_PER_SECOND")
    sec_newsroom_requests_per_second: float = Field(
        default=0.1, alias="SEC_NEWSROOM_REQUESTS_PER_SECOND")
    sec_scrape_cron: str = Field(
        default="0 6,14,22 * * *", alias="SEC_SCRAPE_CRON")
    finra_scrape_cron: str = Field(
        default="10 6,14,22 * * *", alias="FINRA_SCRAPE_CRON")
    cftc_scrape_cron: str = Field(
        default="20 6,14,22 * * *", alias="CFTC_SCRAPE_CRON")
    nfa_scrape_cron: str = Field(
        default="30 6,14,22 * * *", alias="NFA_SCRAPE_CRON")
    policy_re_evaluation_lookback_days: int = Field(
        default=90, alias="POLICY_RE_EVALUATION_LOOKBACK_DAYS")
    auto_generate_redlines: bool = Field(
        default=False, alias="AUTO_GENERATE_REDLINES")
    redline_min_vector_similarity: float = Field(
        default=0.25, alias="REDLINE_MIN_VECTOR_SIMILARITY")
    redline_vector_validation_max_retries: int = Field(
        default=4, alias="REDLINE_VECTOR_VALIDATION_MAX_RETRIES")
    auto_generate_compliance_report: bool = Field(
        default=True, alias="AUTO_GENERATE_COMPLIANCE_REPORT")
    max_policy_upload_mb: int = Field(default=50, alias="MAX_POLICY_UPLOAD_MB")
    business_change_review_types: str = Field(
        default="NEW_PRODUCT,NEW_PROCESS,NEW_HIRE,NEW_MARKET",
        alias="BUSINESS_CHANGE_REVIEW_TYPES",
    )
    sla_reminder_days: int = Field(default=3, alias="SLA_REMINDER_DAYS")
    sla_reminder_cron: str = Field(
        default="0 9 * * *", alias="SLA_REMINDER_CRON")
    escalation_cron: str = Field(default="0 10 * * *", alias="ESCALATION_CRON")
    material_gap_redline_cron: str = Field(
        default="0 7,13,19 * * 1-5", alias="MATERIAL_GAP_REDLINE_CRON")
    regulatory_compliance_workflow_cron: str = Field(
        default="TZ=America/New_York 0 4,14 * * *",
        alias="REGULATORY_COMPLIANCE_WORKFLOW_CRON",
    )
    reupload_reminder_grace_hours: int = Field(
        default=2, alias="REUPLOAD_REMINDER_GRACE_HOURS")
    reupload_reminder_cron: str = Field(
        default="0 * * * *", alias="REUPLOAD_REMINDER_CRON")
    newsletter_token_secret: str | None = Field(
        default=None, alias="NEWSLETTER_TOKEN_SECRET")
    weekly_newsletter_cron: str = Field(
        default="TZ=America/New_York 0 8 * * 1",
        alias="WEEKLY_NEWSLETTER_CRON",
    )

    @field_validator("database_url", "direct_url", mode="before")
    @classmethod
    def sanitize_postgres_urls(cls, value: object) -> object:
        """Strip Prisma-only DSN options (e.g. ?pgbouncer=true) for SQLAlchemy/psycopg."""
        if value is None or not isinstance(value, str):
            return value
        from app.db.url_utils import sanitize_database_url

        return sanitize_database_url(value)

    @field_validator("anthropic_opus_model")
    @classmethod
    def normalize_anthropic_opus_model(cls, value: str) -> str:
        """Map retired or display-style Opus names to a supported Anthropic model."""
        aliases = {
            "claude-opus-4": "claude-opus-4-6",
            "claude-opus-4.0": "claude-opus-4-6",
            "claude-opus-4-20250514": "claude-opus-4-6",
            "claude-opus-4.6": "claude-opus-4-6",
            "claude-opus-4.7": "claude-opus-4-7",
            "claude-opus-4.8": "claude-opus-4-8",
        }
        return aliases.get(value.strip().lower(), value.strip())

    @field_validator("anthropic_sonnet_model")
    @classmethod
    def normalize_anthropic_sonnet_model(cls, value: str) -> str:
        """Map display-style Sonnet names to a supported Anthropic model."""
        aliases = {
            "claude-sonnet-4": "claude-sonnet-4-6",
            "claude-sonnet-4.5": "claude-sonnet-4-6",
            "claude-sonnet-4-5": "claude-sonnet-4-6",
            "claude-sonnet-4-5-20250929": "claude-sonnet-4-6",
            "claude-sonnet-4.6": "claude-sonnet-4-6",
            "claude-sonnet-5": "claude-sonnet-5",
        }
        return aliases.get(value.strip().lower(), value.strip())

    @property
    def resolved_newsletter_token_secret(self) -> str:
        """HMAC secret for newsletter invite links."""
        if self.newsletter_token_secret:
            return str(self.newsletter_token_secret)
        if self.supabase_service_role_key:
            return str(self.supabase_service_role_key)
        return "reg-ai-newsletter-dev"

    @property
    def resolved_anthropic_gap_analysis_model(self) -> str:
        """Use Opus for gap analysis unless ANTHROPIC_GAP_ANALYSIS_MODEL is set."""
        if self.anthropic_gap_analysis_model:
            return self.anthropic_gap_analysis_model
        return self.anthropic_opus_model

    @property
    def resolved_anthropic_redline_model(self) -> str:
        """Use Sonnet for redline drafting unless ANTHROPIC_REDLINE_MODEL is set."""
        if self.anthropic_redline_model:
            return self.anthropic_redline_model
        return self.anthropic_sonnet_model

    @property
    def cors_origin_list(self) -> list[str]:
        """Return CORS origins from CORS_ORIGINS and FRONTEND_URL."""
        origins: list[str] = []
        seen: set[str] = set()
        for raw in (
            str(self.cors_origins),
            str(self.frontend_url),
            str(self.clerk_authorized_parties),
        ):
            for origin in raw.split(","):
                normalized = origin.strip().rstrip("/")
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    origins.append(normalized)
        return origins

    @property
    def public_frontend_url(self) -> str:
        """HTTPS origin for emailed links. Never localhost or a private LAN host."""
        for candidate in (self.frontend_url, *self.cors_origin_list):
            origin = str(candidate or "").strip().rstrip("/")
            if is_public_frontend_origin(origin):
                return origin
        if self.is_production:
            return PUBLIC_FRONTEND_URL_PRODUCTION
        if self.is_staging:
            return PUBLIC_FRONTEND_URL_STAGING
        return PUBLIC_FRONTEND_URL_DEV

    @property
    def cors_frontend_configured(self) -> bool:
        """True when at least one public HTTPS frontend origin is allowed."""
        for origin in self.cors_origin_list:
            if ".vercel.app" in origin:
                return True
            if origin.startswith("https://") and "localhost" not in origin and "127.0.0.1" not in origin:
                return True
        return False

    @property
    def cors_origin_regex(self) -> str | None:
        """Allow local dev origins; also allow Vercel preview URLs when configured."""
        vercel_origin_regex = r"https://[\w-]+(\.[\w-]+)*\.vercel\.app"
        local_hosts = (
            r"localhost|"
            r"127\.0\.0\.1|"
            r"\[::1\]|"
            r"10(?:\.\d{1,3}){3}|"
            r"172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}|"
            r"192\.168(?:\.\d{1,3}){2}"
        )
        local_origin_regex = rf"https?://({local_hosts})(:\d+)?"
        allow_vercel = any(".vercel.app" in origin for origin in self.cors_origin_list)
        if self.is_production:
            return vercel_origin_regex if allow_vercel else None
        if allow_vercel:
            return rf"({vercel_origin_regex})|({local_origin_regex})"
        return local_origin_regex

    @property
    def clerk_authorized_party_list(self) -> list[str]:
        """Deprecated: formerly Clerk authorized parties."""
        return [party.strip() for party in str(self.clerk_authorized_parties).split(",") if party.strip()]

    @property
    def database_use_transaction_pooler(self) -> bool:
        """Detect Supabase transaction pooler URLs (port 6543)."""
        url = self.database_url or ''
        return ':6543/' in url or ':6543?' in url

    @property
    def resolved_supabase_jwt_issuer(self) -> str | None:
        if self.supabase_jwt_issuer:
            return str(self.supabase_jwt_issuer).rstrip('/')
        if self.supabase_url:
            return f"{str(self.supabase_url).rstrip('/')}/auth/v1"
        if self.clerk_issuer:
            return str(self.clerk_issuer).rstrip('/')
        return None

    @property
    def resolved_supabase_jwks_url(self) -> str | None:
        if self.supabase_jwks_url:
            return self.supabase_jwks_url
        issuer = self.resolved_supabase_jwt_issuer
        if issuer:
            return f"{issuer}/.well-known/jwks.json"
        return self.clerk_jwks_url

    @property
    def supabase_jwt_issuer_values(self) -> list[str] | None:
        issuer = self.resolved_supabase_jwt_issuer
        if not issuer:
            return None
        return [issuer, f"{issuer}/"]

    @property
    def clerk_issuer_values(self) -> list[str] | None:
        """Deprecated alias for supabase_jwt_issuer_values."""
        return self.supabase_jwt_issuer_values

    @property
    def supabase_storage_bucket_list(self) -> list[str]:
        """Return the list of Supabase storage buckets from the comma-separated config value."""
        return [bucket.strip() for bucket in str(self.supabase_storage_buckets).split(",") if bucket.strip()]

    @property
    def business_change_review_type_set(self) -> set[str]:
        """Return a normalized set of business change review types."""
        return {
            change_type.strip().upper()
            for change_type in str(self.business_change_review_types).split(",")
            if change_type.strip()
        }

    @property
    def sec_newsroom_url_list(self) -> list[str]:
        return comma_separated_urls(self.sec_newsroom_urls)

    @property
    def finra_rss_url_list(self) -> list[str]:
        return comma_separated_urls(self.finra_rss_urls)

    @property
    def cftc_rss_url_list(self) -> list[str]:
        return comma_separated_urls(self.cftc_rss_urls)

    @property
    def nfa_rss_url_list(self) -> list[str]:
        return comma_separated_urls(self.nfa_rss_urls)

    @property
    def supabase_auth_configured(self) -> bool:
        """Return True when Supabase Auth JWT verification can run."""
        return bool(self.resolved_supabase_jwks_url and self.resolved_supabase_jwt_issuer)

    @property
    def clerk_configured(self) -> bool:
        """Deprecated: use supabase_auth_configured."""
        return self.supabase_auth_configured

    @property
    def supabase_configured(self) -> bool:
        """Return True when Supabase storage/API credentials are present."""
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def stripe_configured(self) -> bool:
        return bool(self.stripe_secret_key)

    @property
    def tenant_provisioning_configured(self) -> bool:
        """True when this environment can provision an isolated org data plane.

        Same workflow on local, dev, staging, and production. MASTER_ACCOUNT
        and internal orgs still bind to the shared public schema; other
        customer orgs get ``{organization_name}_tenant`` when this flag is on.
        """
        if not self.tenant_provisioning_enabled:
            return False
        mode = str(self.tenant_isolation_mode or "schema").strip().lower()
        if mode == "project":
            return bool(self.supabase_access_token and self.supabase_platform_org_slug)
        return bool(self.database_url or self.direct_url)

    @property
    def is_railway(self) -> bool:
        """Return True when the process is running on Railway."""
        return bool(
            os.environ.get('RAILWAY_PROJECT_ID')
            or os.environ.get('RAILWAY_ENVIRONMENT_NAME')
            or os.environ.get('RAILWAY_SERVICE_ID')
        )

    @property
    def deployment_ready(self) -> bool:
        """True when production Railway/Vercel integration prerequisites are met."""
        return bool(
            self.is_production
            and not self.auth_bypass_enabled
            and self.cors_frontend_configured
            and self.database_url
            and self.supabase_auth_configured
            and self.supabase_configured
        )

    @property
    def is_production(self) -> bool:
        """Return True when running in a production environment."""
        return str(self.environment).lower() in {"production", "prod"}

    @property
    def is_staging(self) -> bool:
        """Return True when running in a dedicated staging/test environment."""
        return str(self.environment).lower() in {"staging", "stage", "test"}

    @property
    def allowed_ip_list(self) -> list[str]:
        return [item.strip() for item in str(self.allowed_ips).split(",") if item.strip()]

    @property
    def allowed_ip_cidr_list(self) -> list[str]:
        return [item.strip() for item in str(self.allowed_ip_cidrs).split(",") if item.strip()]

    @property
    def ip_allowlist_enabled(self) -> bool:
        """True when at least one exact IP or CIDR is configured."""
        return bool(self.allowed_ip_list or self.allowed_ip_cidr_list)

    @property
    def inngest_async_dispatch_enabled(self) -> bool:
        """Use Inngest Cloud for async workflows only when fully configured in production."""
        return bool(
            self.inngest_event_key
            and self.inngest_signing_key
            and self.is_production
        )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()


def comma_separated_urls(value: str) -> list[str]:
    """Return valid HTTP(S) URLs from a comma-separated environment value."""
    return [url for item in str(value).split(",") if (url := item.strip()).startswith(("https://", "http://"))]
