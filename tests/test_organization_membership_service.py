"""Tests for local organization membership management (no external IdP)."""

from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock

from app.core.roles import CHIEF_COMPLIANCE_OFFICER, NOMINATED_TEAM_MEMBER
from app.models.core import Organization, OrganizationMember
from app.services.organization_membership_service import (
    assign_cco_role,
    assign_nominated_team_member_role,
    list_organization_members,
    remove_organization_member,
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

    def order_by(self, *_args: object) -> FakeSession:
        return self

    def _rows(self) -> list[object]:
        rows: list[object]
        if self._model is Organization:
            rows = list(self.orgs)
        elif self._model is OrganizationMember:
            rows = list(self.members)
        else:
            rows = []
        for key, value in self._filters.items():
            rows = [row for row in rows if getattr(row, key, None) == value]
        return rows

    def one_or_none(self) -> object | None:
        rows = self._rows()
        return rows[0] if rows else None

    def all(self) -> list[object]:
        return self._rows()

    def add(self, row: object) -> None:
        if isinstance(row, Organization):
            self.orgs.append(row)
        elif isinstance(row, OrganizationMember):
            self.members.append(row)

    def delete(self, row: object) -> None:
        if isinstance(row, OrganizationMember):
            self.members = [item for item in self.members if item is not row]

    def begin_nested(self) -> nullcontext[None]:
        return nullcontext()

    def flush(self, _objects: object = None) -> None:
        return None

    def expunge(self, row: object) -> None:
        self.delete(row)


def test_assign_cco_role_creates_local_membership(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.frontend_url = 'http://localhost:3000'
    monkeypatch.setattr('app.core.config.get_settings', lambda: settings)
    monkeypatch.setattr(
        'app.services.org_lifecycle_service._queue_tenant_provision',
        lambda *_args, **_kwargs: None,
    )
    db = FakeSession()
    member = assign_cco_role(db, auth_org_id='org_example', cco_user_id='user_cco')  # type: ignore[arg-type]

    assert member.auth_org_id == 'org_example'
    assert member.auth_user_id == 'user_cco'
    assert member.role == CHIEF_COMPLIANCE_OFFICER
    assert len(db.orgs) == 1


def test_assign_nominated_team_member_role_promotes_existing_member() -> None:
    db = FakeSession()
    db.orgs.append(Organization(id='row', auth_org_id='org_example', name='Example'))
    db.members.append(
        OrganizationMember(
            id='member-1',
            auth_org_id='org_example',
            auth_user_id='user_1',
            role='nominated_team_member',
            status='active',
        )
    )

    member = assign_nominated_team_member_role(db, auth_org_id='org_example', user_id='user_1')  # type: ignore[arg-type]

    assert member.role == NOMINATED_TEAM_MEMBER
    assert len(db.members) == 1


def test_list_organization_members_returns_local_rows() -> None:
    db = FakeSession()
    db.members.append(
        OrganizationMember(
            id='member-1',
            auth_org_id='org_example',
            auth_user_id='user_1',
            role='chief_compliance_officer',
            status='active',
            email='admin@example.com',
            full_name='Admin User',
        )
    )

    members = list_organization_members(db, 'org_example')  # type: ignore[arg-type]

    assert len(members) == 1
    assert members[0].user_id == 'user_1'
    assert members[0].email == 'admin@example.com'
    assert members[0].role == 'chief_compliance_officer'


def test_remove_organization_member_deletes_row() -> None:
    db = FakeSession()
    db.members.append(
        OrganizationMember(
            id='member-1',
            auth_org_id='org_example',
            auth_user_id='user_1',
            role='nominated_team_member',
            status='active',
        )
    )

    removed = remove_organization_member(db, auth_org_id='org_example', user_id='user_1')  # type: ignore[arg-type]

    assert removed is True
    assert db.members == []
