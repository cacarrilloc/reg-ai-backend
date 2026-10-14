from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

import app.core.inngest as inngest_module
from app.core.config import Settings


@pytest.mark.filterwarnings(
    "ignore:Support for class-based `config` is deprecated.*:pydantic.warnings.PydanticDeprecatedSince20"
)
def test_inngest_uses_versioned_api_prefix(monkeypatch) -> None:
    """Ensure Inngest registers routes under the configured API v1 prefix."""
    settings = Settings(
        API_V1_PREFIX="/api/v1",
        INNGEST_EVENT_KEY="test-event-key",
        INNGEST_SIGNING_KEY="test-signing-key",
    )
    monkeypatch.setattr(inngest_module, "get_settings", lambda: settings)
    app = FastAPI()

    inngest_module.register_inngest(app)

    routes = [route for route in app.routes if isinstance(route, APIRoute) and route.path == "/api/v1/inngest"]
    methods = {method for route in routes for method in route.methods or set()}
    assert methods == {"GET", "POST", "PUT"}


def test_inngest_registers_review_cron_jobs(monkeypatch) -> None:
    settings = Settings(
        API_V1_PREFIX="/api/v1",
        INNGEST_EVENT_KEY="test-event-key",
        INNGEST_SIGNING_KEY="test-signing-key",
        SLA_REMINDER_CRON="0 9 * * *",
        ESCALATION_CRON="0 10 * * *",
    )
    monkeypatch.setattr(inngest_module, "get_settings", lambda: settings)

    created: list[dict[str, object]] = []

    class FakeInngest:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def create_function(self, fn_id: str, trigger: object, idempotency: str | None = None):
            created.append({"fn_id": fn_id, "trigger": trigger, "idempotency": idempotency})

            def decorator(handler: object) -> object:
                return handler

            return decorator

    def fake_serve(_app: FastAPI, _client: object, functions: list[object], serve_path: str) -> None:
        created.append({"serve_path": serve_path, "function_count": len(functions)})

    fake_inngest_module = SimpleNamespace(
        Inngest=FakeInngest,
        TriggerCron=lambda cron: SimpleNamespace(cron=cron),
        TriggerEvent=lambda event: SimpleNamespace(event=event),
    )

    monkeypatch.setitem(__import__("sys").modules, "inngest", fake_inngest_module)
    monkeypatch.setitem(__import__("sys").modules, "inngest.fast_api", SimpleNamespace(serve=fake_serve))
    monkeypatch.setattr(inngest_module, "inngest", fake_inngest_module)

    app = FastAPI()
    inngest_module.register_inngest(app)

    fn_ids = [item["fn_id"] for item in created if "fn_id" in item]
    assert "regulatory-compliance-workflow" in fn_ids
    assert "review-sla-reminder" in fn_ids
    assert "review-escalation" in fn_ids
    assert "weekly-newsletter" in fn_ids
    assert any(
        getattr(item["trigger"], "cron", None) == "TZ=America/New_York 0 8 * * 1"
        for item in created
        if "trigger" in item and item.get("fn_id") == "weekly-newsletter"
    )
    assert any(
        getattr(item["trigger"], "cron", None) == "0 9 * * *"
        for item in created
        if "trigger" in item and item.get("fn_id") == "review-sla-reminder"
    )
    assert any(
        getattr(item["trigger"], "cron", None) == "0 10 * * *"
        for item in created
        if "trigger" in item and item.get("fn_id") == "review-escalation"
    )
