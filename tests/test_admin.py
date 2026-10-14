from collections.abc import Generator

from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app
from app.schemas.admin import ClientOrganizationCreate
from app.services.admin_service import assign_cco_role_for_client_org


def override_db_none() -> Generator[None, None, None]:
    yield None


def test_create_client_org_dev_bypass() -> None:
    app.dependency_overrides[get_db] = override_db_none
    client = TestClient(app)
    response = client.post(
        '/api/v1/admin/client-orgs',
        json={
            'name': 'Example Capital',
            'auth_org_id': 'org_example',
            'cco_user_id': 'user_cco',
            'regulatory_scope': {
                'sources': ['SEC', 'FINRA'],
                'jurisdictions': ['US'],
                'products': ['RIA'],
            },
        },
        headers={'x-user-role': 'Admin User'},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['name'] == 'Example Capital'
    assert payload['auth_org_id'] == 'org_example'
    assert payload['cco_user_id'] == 'user_cco'
    assert payload['persisted'] is False
    assert payload['cco_role'] == 'chief_compliance_officer'
    assert payload['role_assigned'] is False
    app.dependency_overrides.clear()


def test_create_client_org_requires_internal_role() -> None:
    app.dependency_overrides[get_db] = override_db_none
    client = TestClient(app)
    response = client.post(
        '/api/v1/admin/client-orgs',
        json={
            'name': 'Example Capital',
            'auth_org_id': 'org_example',
            'cco_user_id': 'user_cco',
        },
        headers={'x-user-role': 'Product Manager'},
    )

    assert response.status_code == 403
    app.dependency_overrides.clear()


def test_client_org_create_does_not_fail_when_role_assignment_fails(monkeypatch) -> None:
    def fail_assignment(*_args: object, **_kwargs: object) -> None:
        raise ValueError('auth_org_id and auth_user_id are required')

    monkeypatch.setattr('app.services.admin_service.assign_cco_role', fail_assignment)

    class DbStub:
        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    assigned = assign_cco_role_for_client_org(
        ClientOrganizationCreate(
            name='Example Capital',
            auth_org_id='org_example',
            cco_user_id='user_cco',
        ),
        DbStub(),  # type: ignore[arg-type]
    )

    assert assigned is False
