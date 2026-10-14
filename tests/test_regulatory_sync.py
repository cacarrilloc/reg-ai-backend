from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app


def test_regulatory_sync_requires_elevated_role() -> None:
    def fake_db():
        yield object()

    app.dependency_overrides[get_db] = fake_db
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/regulatory/sync',
            headers={'x-org-id': 'test-org', 'x-user-role': 'guest'},
        )

        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_regulatory_sync_accepts_cco_role(monkeypatch) -> None:
    scheduled: list[str | None] = []

    async def fake_background(org_id: str | None = None) -> None:
        scheduled.append(org_id)

    monkeypatch.setattr(
        'app.api.v1.endpoints.regulatory.run_regulatory_sync_background',
        fake_background,
    )
    monkeypatch.setattr(
        'app.api.v1.endpoints.regulatory.count_active_internal_policies',
        lambda db, org_id=None: 1,
    )

    def fake_db():
        yield object()

    app.dependency_overrides[get_db] = fake_db
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/regulatory/sync',
            headers={'x-org-id': 'test-org', 'x-user-role': 'chief_compliance_officer'},
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload['status'] == 'accepted'
        assert 'Regulatory compliance workflow started' in payload['message']
        assert 'sec.gov/newsroom' in payload['message']
        assert 'finra.org/media-center' in payload['message']
        assert 'cftc.gov/PressRoom' in payload['message']
        assert scheduled == ['test-org']
    finally:
        app.dependency_overrides.clear()


def test_regulatory_sync_scrapes_marketing_rules_without_internal_policies(monkeypatch) -> None:
    scheduled: list[str] = []
    workflow: list[str | None] = []

    async def fake_background(org_id: str | None = None) -> None:
        workflow.append(org_id)

    monkeypatch.setattr(
        'app.api.v1.endpoints.regulatory.run_regulatory_sync_background',
        fake_background,
    )
    monkeypatch.setattr(
        'app.api.v1.endpoints.regulatory.count_active_internal_policies',
        lambda db, org_id=None: 0,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.schedule_marketing_regulation_sync',
        scheduled.append,
    )

    def fake_db():
        yield object()

    app.dependency_overrides[get_db] = fake_db
    try:
        client = TestClient(app)
        response = client.post(
            '/api/v1/regulatory/sync',
            headers={'x-org-id': 'test-org', 'x-user-role': 'chief_compliance_officer'},
        )

        payload = response.json()
        assert response.status_code == 202
        assert payload['status'] == 'accepted'
        assert 'marketing' in payload['message'].lower()
        assert scheduled == ['test-org']
        assert workflow == []
    finally:
        app.dependency_overrides.clear()
