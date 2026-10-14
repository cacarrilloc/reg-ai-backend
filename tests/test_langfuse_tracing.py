from app.core.langfuse_tracing import (
    api_feature_from_path,
    api_trace_name,
    should_trace_request,
)


def test_should_trace_request_skips_health_and_docs() -> None:
    assert should_trace_request('/api/v1/health') is False
    assert should_trace_request('/health') is False
    assert should_trace_request('/docs') is False
    assert should_trace_request('/openapi.json') is False


def test_should_trace_request_includes_api_routes() -> None:
    assert should_trace_request('/api/v1/gap-analyses') is True
    assert should_trace_request('/api/v1/reviews/pending') is True


def test_api_trace_name_uses_method_and_feature() -> None:
    assert api_trace_name('GET', '/api/v1/gap-analyses') == 'api.get.gap-analyses'
    assert api_feature_from_path('/api/v1/reviews/pending') == 'reviews'
