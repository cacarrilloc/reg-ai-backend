"""Tests for the legacy organization-sync compatibility shims."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock

from app.models.core import Organization, OrganizationMember
from app.services.clerk_org_sync_service import (
    handle_clerk_organization_event,
    upsert_organization_from_clerk,
    upsert_organization_member,
)


def _allow_customer_org_create(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.frontend_url = 'http://localhost:3000'
    monkeypatch.setattr('app.core.config.get_settings', lambda: settings)
    monkeypatch.setattr(
        'app.services.org_lifecycle_service._queue_tenant_provision',
        lambda *_args, **_kwargs: None,
    )


class FakeSession:
    def __init__(self) -> None:
        self.orgs: list[Organization] = []
        self.members: list[OrganizationMember] = []
        self._model: type | None = None
        self._filters: dict[str, object] = {}

    def query(self, model: type) -> FakeSession:
        self._model = model
        self._filters = {}
        return self

    def filter(self, *args: object, **_kwargs: object) -> FakeSession:
        for arg in args:
            left = getattr(arg, 'left', None)
            right = getattr(arg, 'right', None)
            key = getattr(left, 'key', None) or getattr(left, 'name', None)
            value = getattr(right, 'value', right)
            if key is not None:
                self._filters[str(key)] = value
        return self

    def _filtered_rows(self) -> list[object]:
        if self._model is Organization:
            rows: list[object] = list(self.orgs)
        elif self._model is OrganizationMember:
            rows = list(self.members)
        else:
            return []
        for key, value in self._filters.items():
            rows = [row for row in rows if getattr(row, key, None) == value]
        return rows

    def one_or_none(self) -> object | None:
        rows = self._filtered_rows()
        return rows[0] if rows else None

    def all(self) -> list[object]:
        return self._filtered_rows()

    def one(self) -> object:
        row = self.one_or_none()
        if row is None:
            raise AssertionError('no row found')
        return row

    def add(self, row: object) -> None:
        if isinstance(row, Organization):
            self.orgs.append(row)
        elif isinstance(row, OrganizationMember):
            self.members.append(row)

    def begin_nested(self) -> nullcontext[None]:
        return nullcontext()

    def flush(self, _objects: object = None) -> None:
        return None

    def expunge(self, row: object) -> None:
        self.delete(row)

    def delete(self, row: object) -> None:
        if isinstance(row, Organization):
            self.orgs = [item for item in self.orgs if item is not row]
        elif isinstance(row, OrganizationMember):
            self.members = [item for item in self.members if item is not row]


def test_upsert_organization_from_clerk_creates_profile(monkeypatch) -> None:
    _allow_customer_org_create(monkeypatch)
    db = FakeSession()
    org = upsert_organization_from_clerk(
        db,  # type: ignore[arg-type]
        auth_org_id='org_clerk_1',
        name='Acme Compliance',
    )
    assert org.auth_org_id == 'org_clerk_1'
    assert org.name == 'Acme Compliance'
    assert len(db.orgs) == 1


def test_upsert_organization_member_creates_sub_user(monkeypatch) -> None:
    _allow_customer_org_create(monkeypatch)
    db = FakeSession()
    member = upsert_organization_member(
        db,  # type: ignore[arg-type]
        auth_org_id='org_clerk_2',
        auth_user_id='user_abc',
        role='chief_compliance_officer',
        email='ada@example.com',
        full_name='Ada Lovelace',
        org_name='Beta LLC',
    )
    assert len(db.orgs) == 1
    assert db.orgs[0].name == 'Beta LLC'
    assert member.auth_user_id == 'user_abc'
    assert member.role == 'chief_compliance_officer'
    assert member.email == 'ada@example.com'


def test_membership_webhook_event_upserts_member(monkeypatch) -> None:
    _allow_customer_org_create(monkeypatch)
    db = FakeSession()
    action = handle_clerk_organization_event(
        db,  # type: ignore[arg-type]
        'organizationMembership.created',
        {
            'role': 'org:chief_compliance_officer',
            'organization': {'id': 'org_clerk_3', 'name': 'Gamma Inc'},
            'public_user_data': {
                'user_id': 'user_xyz',
                'first_name': 'Grace',
                'last_name': 'Hopper',
                'identifier': 'grace@example.com',
            },
        },
    )
    assert action == 'membership_upserted'
    assert db.orgs[0].auth_org_id == 'org_clerk_3'
    assert db.members[0].role == 'org:chief_compliance_officer'
