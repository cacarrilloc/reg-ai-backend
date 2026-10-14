from fastapi import Request
from fastapi.testclient import TestClient

import app.api.v1.endpoints.health as health_endpoint
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.main import app
from app.models.core import DocumentChunk


def test_health_check() -> None:
    client = TestClient(app)
    response = client.get('/api/v1/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_top_level_health_check() -> None:
    client = TestClient(app)
    response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_health_check_reports_deployment_flags(monkeypatch) -> None:
    monkeypatch.setattr(
        health_endpoint,
        'get_settings',
        lambda: Settings(
            ENVIRONMENT='production',
            AUTH_BYPASS_ENABLED=False,
            CORS_ORIGINS='https://reg-ai-frontend.vercel.app',
            DATABASE_URL='postgresql://user:pass@localhost:5432/postgres',
            CLERK_ISSUER='https://example.clerk.accounts.dev',
            CLERK_JWKS_URL='https://example.clerk.accounts.dev/.well-known/jwks.json',
            SUPABASE_URL='https://example.supabase.co',
            SUPABASE_SERVICE_ROLE_KEY='service-role-key',
        ),
    )
    monkeypatch.setattr(health_endpoint, 'probe_database', lambda: (True, True, None))

    response = TestClient(app).get('/api/v1/health')
    body = response.json()

    assert response.status_code == 200
    assert body['cors_frontend_configured'] is True
    assert body['clerk_configured'] is True
    assert body['supabase_configured'] is True
    assert body['auth_bypass_enabled'] is False
    assert body['deployment_ready'] is True


def test_favicon_request_does_not_404() -> None:
    client = TestClient(app)
    response = client.get('/favicon.ico')
    assert response.status_code == 204


def test_multipart_parser_allows_policy_upload_size() -> None:
    import inspect

    default = inspect.signature(Request.form).parameters['max_part_size'].default
    assert default == get_settings().max_policy_upload_mb * 1024 * 1024


class CountQuery:
    def __init__(self, total_count: int, embedded_count: int) -> None:
        self.total_count = total_count
        self.embedded_count = embedded_count
        self.embedded_filter_seen = False

    def join(self, *_args: object) -> 'CountQuery':
        return self

    def filter(self, *criteria: object) -> 'CountQuery':
        query = CountQuery(self.total_count, self.embedded_count)
        query.embedded_filter_seen = self.embedded_filter_seen or any(
            'document_chunks.embedding' in str(criterion) for criterion in criteria
        )
        return query

    def count(self) -> int:
        return self.embedded_count if self.embedded_filter_seen else self.total_count


class CountSession:
    def __init__(self, total_count: int, embedded_count: int) -> None:
        self.total_count = total_count
        self.embedded_count = embedded_count

    def query(self, entity: object) -> CountQuery:
        assert entity is DocumentChunk.id
        return CountQuery(self.total_count, self.embedded_count)


def test_ai_readiness_reports_redacted_provider_and_embedding_status(monkeypatch) -> None:
    monkeypatch.setattr(
        health_endpoint,
        'get_settings',
        lambda: Settings(
            ANTHROPIC_API_KEY='test-anthropic-key',
            ANTHROPIC_OPUS_MODEL='claude-opus-4',
            OPENAI_API_KEY='test-openai-key',
        ),
    )
    app.dependency_overrides[get_db] = lambda: CountSession(
        total_count=5, embedded_count=4)
    try:
        response = TestClient(app).get(
            '/api/v1/health/ai-readiness',
            headers={'x-org-id': 'org_test'},
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body['org_id'] == 'org_test'
    assert body['anthropic_configured'] is True
    assert body['anthropic_opus_model'] == 'claude-opus-4-6'
    assert body['openai_configured'] is True
    assert body['policy_chunk_count'] == 5
    assert body['embedded_policy_chunk_count'] == 4
    assert body['llm_gap_analysis_ready'] is True
    assert body['semantic_retrieval_ready'] is True
    assert body['expected_gap_analysis_mode'] == 'LLM'
    assert body['expected_retrieval_mode'] == 'SEMANTIC_PGVECTOR'
    assert 'test-anthropic-key' not in response.text
    assert 'test-openai-key' not in response.text


def test_ai_readiness_reports_fallback_modes_when_keys_or_embeddings_are_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        health_endpoint,
        'get_settings',
        lambda: Settings(ANTHROPIC_API_KEY='', OPENAI_API_KEY=''),
    )
    app.dependency_overrides[get_db] = lambda: CountSession(
        total_count=5, embedded_count=0)
    try:
        response = TestClient(app).get(
            '/api/v1/health/ai-readiness',
            headers={'x-org-id': 'org_test'},
        )
    finally:
        app.dependency_overrides.clear()

    body = response.json()
    assert response.status_code == 200
    assert body['anthropic_configured'] is False
    assert body['openai_configured'] is False
    assert body['embedded_policy_chunk_count'] == 0
    assert body['llm_gap_analysis_ready'] is False
    assert body['semantic_retrieval_ready'] is False
    assert body['expected_gap_analysis_mode'] == 'RULE_FALLBACK'
    assert body['expected_retrieval_mode'] == 'LEXICAL_FALLBACK'
