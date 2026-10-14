"""Tests for the Stripe/Supabase webhooks and the deprecated Clerk no-op."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app


def test_clerk_webhook_is_a_deprecated_noop() -> None:
    client = TestClient(app)
    response = client.post('/api/v1/webhooks/clerk', json={'type': 'session.created'})
    assert response.status_code == 200
    assert response.json()['status'] == 'ignored'


def test_stripe_webhook_rejects_when_secret_missing(monkeypatch) -> None:
    monkeypatch.delenv('STRIPE_WEBHOOK_SECRET', raising=False)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        '/api/v1/webhooks/stripe',
        content=json.dumps({'type': 'checkout.session.completed'}).encode('utf-8'),
        headers={'content-type': 'application/json', 'stripe-signature': 't=1,v1=abc'},
    )
    assert response.status_code == 503
    config_module.get_settings.cache_clear()


def test_stripe_webhook_rejects_invalid_signature(monkeypatch) -> None:
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET', 'whsec_test_secret')
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        '/api/v1/webhooks/stripe',
        content=json.dumps({'type': 'checkout.session.completed'}).encode('utf-8'),
        headers={'content-type': 'application/json', 'stripe-signature': 't=1,v1=invalid'},
    )
    assert response.status_code == 401
    config_module.get_settings.cache_clear()


def test_supabase_webhook_rejects_when_secret_missing(monkeypatch) -> None:
    monkeypatch.delenv('SUPABASE_WEBHOOK_SECRET', raising=False)
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    client = TestClient(app)
    response = client.post('/api/v1/webhooks/supabase', json={'type': 'user.created'})
    assert response.status_code == 503
    config_module.get_settings.cache_clear()


def test_supabase_webhook_accepts_matching_secret(monkeypatch) -> None:
    monkeypatch.setenv('SUPABASE_WEBHOOK_SECRET', 'shh')
    from app.core import config as config_module

    config_module.get_settings.cache_clear()
    client = TestClient(app)
    response = client.post(
        '/api/v1/webhooks/supabase',
        json={'type': 'user.created'},
        headers={'x-webhook-secret': 'shh'},
    )
    assert response.status_code == 200
    assert response.json()['status'] == 'accepted'
    config_module.get_settings.cache_clear()
