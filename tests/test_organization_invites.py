"""Organization invite role allowlists and email delivery behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.ai.types import AIConfigurationError
from app.core.security import CurrentUser
from app.schemas.organizations import OrganizationInviteAcceptRequest, OrganizationInviteCreate
from app.services import organization_invite_service as invite_service


class _DB:
    def __init__(self) -> None:
        self.deleted = []

    def add(self, *_args, **_kwargs):
        return None

    def commit(self):
        return None

    def refresh(self, invite):
        return None

    def rollback(self):
        return None

    def delete(self, obj):
        self.deleted.append(obj)

    def query(self, *_args, **_kwargs):
        class _EmptyQuery:
            def filter(self, *_a, **_k):
                return self

            def one_or_none(self):
                return None

            def order_by(self, *_a, **_k):
                return self

            def all(self):
                return []

        return _EmptyQuery()


def _user(org_id: str, *, email: str | None = "a@example.com") -> CurrentUser:
    claims = {"email": email} if email else {}
    return CurrentUser(
        user_id="user-1",
        org_id=org_id,
        role="master_user",
        roles=frozenset({"master_user"}),
        claims=claims,
    )


def test_customer_org_allows_master_and_product_manager(monkeypatch) -> None:
    monkeypatch.setattr(invite_service, "_send_invite_email", lambda *_a, **_k: None)
    for role in ("master_user", "product_manager", "chief_compliance_officer", "nominated_team_member"):
        response = invite_service.create_organization_invite(
            OrganizationInviteCreate(email=f"{role}@example.com", role=role),
            _user("org_customer_acme"),
            _DB(),  # type: ignore[arg-type]
        )
        assert response.role == role


def test_developer_invite_role_maps_to_master(monkeypatch) -> None:
    monkeypatch.setattr(invite_service, "_send_invite_email", lambda *_a, **_k: None)
    response = invite_service.create_organization_invite(
        OrganizationInviteCreate(email="dev@example.com", role="developer_user"),
        _user("org_internal_development"),
        _DB(),  # type: ignore[arg-type]
    )
    assert response.role == "master_user"


def test_create_organization_for_user_sets_master_user_active_role(monkeypatch) -> None:
    from app.core.roles import MASTER_USER
    from app.schemas.organizations import OrganizationCreateRequest

    created = SimpleNamespace(
        id="org-row",
        auth_org_id="org_acme",
        name="Acme Compliance",
        slug="acme-compliance",
        kind="external",
    )
    active: dict[str, object] = {}

    monkeypatch.setattr(
        invite_service,
        "create_organization",
        lambda *_args, **_kwargs: created,
    )
    monkeypatch.setattr(
        "app.services.internal_org_service.is_reserved_internal_auth_org_id",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        invite_service,
        "get_settings",
        lambda: SimpleNamespace(),
    )

    def fake_update(user_id, auth_org_id, settings, *, role=None):
        active.update(
            {
                "user_id": user_id,
                "auth_org_id": auth_org_id,
                "role": role,
            }
        )
        return True

    monkeypatch.setattr(invite_service, "update_supabase_active_org", fake_update)
    applied: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "use_default_models",
        lambda _self, org_id, *, actor_id, accept_billing=False: applied.append((org_id, accept_billing)),
    )

    response = invite_service.create_organization_for_user(
        OrganizationCreateRequest(
            name="Acme Compliance",
            ai_setup={"option": "reg_ai_default", "accept_reg_ai_billing": True},
        ),
        _user("org_previous"),
        _DB(),  # type: ignore[arg-type]
    )

    assert response.auth_org_id == "org_acme"
    assert response.ai_setup_applied is True
    assert applied == [("org_acme", True)]
    assert active["role"] == MASTER_USER
    assert active["user_id"] == "user-1"


def _stub_key_staging(monkeypatch) -> dict[str, object]:
    held: dict[str, object] = {}

    def stage(user_id, choices):
        held["order"] = [*held.get("order", []), "stage"]  # type: ignore[operator]
        held["choices"] = list(choices)
        return None

    def bind(user_id, dest_org_id):
        held["order"] = [*held.get("order", []), "bind"]  # type: ignore[operator]
        held["dest"] = dest_org_id

    def load(user_id):
        held["order"] = [*held.get("order", []), "load"]  # type: ignore[operator]
        return list(held.get("choices") or [])

    def discard(user_id):
        held["discarded"] = True

    monkeypatch.setattr(invite_service.key_staging, "stage_workflows", stage)
    monkeypatch.setattr(invite_service.key_staging, "bind_staged_org", bind)
    monkeypatch.setattr(invite_service.key_staging, "staged_dest_org_id", lambda _user_id: held.get("dest"))
    monkeypatch.setattr(invite_service.key_staging, "load_staged_workflows", load)
    monkeypatch.setattr(invite_service.key_staging, "discard_staged_key", discard)
    monkeypatch.setattr(invite_service.key_staging, "db_backed", lambda: False)
    return held


def test_create_organization_stores_the_verified_own_key(monkeypatch) -> None:
    from app.schemas.organizations import OrganizationCreateRequest

    created = SimpleNamespace(
        id="org-row",
        auth_org_id="org_acme",
        name="Acme Compliance",
        slug="acme-compliance",
        kind="external",
    )
    held = _stub_key_staging(monkeypatch)

    def fake_create(*_args, **_kwargs):
        held["order"] = [*held.get("order", []), "create"]  # type: ignore[operator]
        return created

    monkeypatch.setattr(invite_service, "create_organization", fake_create)
    monkeypatch.setattr(
        "app.services.internal_org_service.is_reserved_internal_auth_org_id",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(invite_service, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(invite_service, "update_supabase_active_org", lambda *_a, **_k: True)
    monkeypatch.setattr(invite_service, "_wait_for_ai_data_plane", lambda *_a, **_k: None)
    stored: list[tuple] = []
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "validate_own_key",
        lambda _self, choice: stored.append(("validate", choice.provider, choice.fields)) or None,
    )
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "apply_workflow_choices",
        lambda _self, org_id, choices, *, actor_id: stored.append(
            (org_id, [(choice.provider, dict(choice.fields), choice.role) for choice in choices])
        ),
    )

    response = invite_service.create_organization_for_user(
        OrganizationCreateRequest(
            name="Acme Compliance",
            ai_setup={
                "option": "own_key",
                "workflows": [{
                    "role": "redline",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "fields": {"api_key": "sk-ant-org-9999"},
                }],
            },
        ),
        _user("org_previous"),
        _DB(),  # type: ignore[arg-type]
    )

    assert response.ai_setup_applied is True
    assert held["order"] == ["stage", "create", "bind", "load"]
    assert held.get("dest") == "org_acme"
    assert ("validate", "anthropic", {"api_key": "sk-ant-org-9999"}) in stored
    assert ("org_acme", [("anthropic", {"api_key": "sk-ant-org-9999"}, "redline")]) in stored
    assert held.get("discarded") is True


def test_complete_ai_setup_stores_the_key_after_the_workspace_is_ready(monkeypatch) -> None:
    from app.schemas.organizations import OrganizationCreateRequest

    held = _stub_key_staging(monkeypatch)
    monkeypatch.setattr(invite_service, "_wait_for_ai_data_plane", lambda *_a, **_k: None)
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(invite_service.OrgAISettingsService, "validate_own_key", lambda *_a, **_k: None)
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "apply_workflow_choices",
        lambda _self, org_id, choices, *, actor_id: stored.append((org_id, choices[0].provider)),
    )
    setup = OrganizationCreateRequest(
        name="Acme Compliance",
        ai_setup={
            "option": "own_key",
            "workflows": [{
                "role": "redline",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "fields": {"api_key": "sk-ant-org-9999"},
            }],
        },
    ).ai_setup
    assert invite_service.complete_organization_ai_setup(setup, _user("org_acme")) is True
    assert stored == [("org_acme", "anthropic")]
    assert held.get("discarded") is True


def test_complete_ai_setup_uses_the_key_entered_before_the_org_existed(monkeypatch) -> None:
    from app.ai.settings_service import OwnKeyChoice
    from app.schemas.organizations import OrganizationCreateRequest

    held = _stub_key_staging(monkeypatch)
    held["choices"] = [OwnKeyChoice(
        "anthropic", "claude-sonnet-4-6", "generation", {"api_key": "sk-ant-org-9999"}, role="redline",
    )]
    held["dest"] = "org_new"
    monkeypatch.setattr(invite_service, "_wait_for_ai_data_plane", lambda *_a, **_k: None)
    stored: list[tuple[str, str]] = []
    monkeypatch.setattr(invite_service.OrgAISettingsService, "validate_own_key", lambda *_a, **_k: None)
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "apply_workflow_choices",
        lambda _self, org_id, choices, *, actor_id: stored.append((org_id, choices[0].provider)),
    )
    setup = OrganizationCreateRequest(
        name="Acme Compliance",
        ai_setup={
            "option": "own_key",
            "workflows": [{
                "role": "redline",
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
            }],
        },
    ).ai_setup
    assert invite_service.complete_organization_ai_setup(setup, _user("org_stale_jwt")) is True
    assert stored == [("org_new", "anthropic")]
    assert held.get("discarded") is True


def test_organization_cannot_be_created_without_an_ai_model_choice() -> None:
    from pydantic import ValidationError

    from app.schemas.organizations import OrganizationCreateRequest

    with pytest.raises(ValidationError):
        OrganizationCreateRequest(name="Acme Compliance")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        OrganizationCreateRequest(name="Acme Compliance", ai_setup={"option": "reg_ai_default"})
    with pytest.raises(ValidationError):
        OrganizationCreateRequest(name="Acme Compliance", ai_setup={"option": "own_key"})


def test_invalid_registration_key_blocks_org_creation(monkeypatch) -> None:
    from app.schemas.organizations import OrganizationCreateRequest

    def must_not_create(*_args, **_kwargs):
        raise AssertionError("organization must not be created with an invalid key")

    monkeypatch.setattr(invite_service, "create_organization", must_not_create)
    monkeypatch.setattr(
        invite_service.OrgAISettingsService,
        "validate_own_key",
        lambda _self, _choice: (_ for _ in ()).throw(AIConfigurationError("Missing credential fields: api_key")),
    )
    with pytest.raises(HTTPException) as exc_info:
        invite_service.create_organization_for_user(
            OrganizationCreateRequest(
                name="Acme Compliance",
                ai_setup={
                    "option": "own_key",
                    "workflows": [{"role": "redline", "provider": "openai", "model": "gpt-4o"}],
                },
            ),
            _user("org_previous"),
            _DB(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 422
    assert "api_key" in str(exc_info.value.detail)


def test_create_invite_fails_and_deletes_when_email_not_sent(monkeypatch) -> None:
    def fail_send(_invite, **_kwargs) -> None:
        raise HTTPException(
            status_code=502,
            detail="Invite was not emailed to a@example.com: Resend API key not configured",
        )

    monkeypatch.setattr(invite_service, "_send_invite_email", fail_send)
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        invite_service.create_organization_invite(
            OrganizationInviteCreate(email="a@example.com", role="master_user"),
            _user("org_internal_development"),
            db,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 502
    assert "not emailed" in str(exc.value.detail)
    assert len(db.deleted) == 1


def test_send_invite_email_raises_on_failed_resend(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.notification_service.send_resend_email",
        lambda **_kwargs: {"status": "failed", "error": "delivery rejected"},
    )
    monkeypatch.setattr(
        invite_service,
        "get_settings",
        lambda: SimpleNamespace(frontend_url="http://localhost:3000"),
    )
    invite = SimpleNamespace(
        id="invite-1",
        email="sinrepresion@gmail.com",
        role="master_user",
        token="token-abc",
    )
    with pytest.raises(HTTPException) as exc:
        invite_service._send_invite_email(invite)  # type: ignore[arg-type]
    assert exc.value.status_code == 502
    assert "sinrepresion@gmail.com" in str(exc.value.detail)
    assert "delivery rejected" in str(exc.value.detail)


def test_accept_invite_sets_full_name_from_metadata_or_email(monkeypatch) -> None:
    invite = SimpleNamespace(
        id="invite-1",
        auth_org_id="org_customer_acme",
        email="sinrepresion@gmail.com",
        role="master_user",
        token="token-abc",
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
    )
    captured: dict[str, object] = {}

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def one_or_none(self):
            return invite

    class _AcceptDB(_DB):
        def query(self, *_a, **_k):
            return _Query()

    def fake_upsert(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(invite_service, "upsert_member", fake_upsert)
    monkeypatch.setattr(invite_service, "update_supabase_active_org", lambda *_a, **_k: True)

    user = CurrentUser(
        user_id="user-new",
        org_id="org_other",
        role="nominated_team_member",
        roles=frozenset({"nominated_team_member"}),
        claims={
            "email": "sinrepresion@gmail.com",
            "user_metadata": {"username": "sinre"},
        },
    )
    response = invite_service.accept_organization_invite(
        OrganizationInviteAcceptRequest(token="token-abc"),
        user,
        _AcceptDB(),  # type: ignore[arg-type]
    )
    assert response.joined is True
    assert captured["full_name"] == "sinre"
    assert invite.status == "accepted"


def test_accept_invite_prefers_invitee_name(monkeypatch) -> None:
    invite = SimpleNamespace(
        id="invite-1",
        auth_org_id="org_customer_acme",
        email="sinrepresion@gmail.com",
        invitee_name="Carlos Rivera",
        role="master_user",
        token="token-abc",
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
    )
    captured: dict[str, object] = {}

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def one_or_none(self):
            return invite

    class _AcceptDB(_DB):
        def query(self, *_a, **_k):
            return _Query()

    def fake_upsert(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(invite_service, "upsert_member", fake_upsert)
    monkeypatch.setattr(invite_service, "update_supabase_active_org", lambda *_a, **_k: True)

    user = CurrentUser(
        user_id="user-new",
        org_id="org_other",
        role="nominated_team_member",
        roles=frozenset({"nominated_team_member"}),
        claims={"email": "sinrepresion@gmail.com"},
    )
    invite_service.accept_organization_invite(
        OrganizationInviteAcceptRequest(token="token-abc"),
        user,
        _AcceptDB(),  # type: ignore[arg-type]
    )
    assert captured["full_name"] == "Carlos Rivera"


def test_claim_email_reads_user_metadata_and_profile() -> None:
    user = CurrentUser(
        user_id="user-new",
        org_id="org_other",
        role="nominated_team_member",
        roles=frozenset({"nominated_team_member"}),
        claims={"user_metadata": {"email": "sinrepresion@gmail.com"}},
    )
    assert invite_service._claim_email(user) == "sinrepresion@gmail.com"

    class _Profile:
        email = "sinrepresion@gmail.com"

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def one_or_none(self):
            return _Profile()

    class _ProfileDB:
        def query(self, *_a, **_k):
            return _Query()

    bare = CurrentUser(
        user_id="user-new",
        org_id="org_other",
        role="nominated_team_member",
        roles=frozenset({"nominated_team_member"}),
        claims={},
    )
    assert invite_service._resolve_user_email(bare, _ProfileDB()) == "sinrepresion@gmail.com"


def test_invite_email_html_includes_role_and_name() -> None:
    invite = SimpleNamespace(
        email="sinrepresion@gmail.com",
        invitee_name="Carlos",
        role="master_user",
        token="token-abc",
    )
    html = invite_service._build_invite_email_html(
        invite=invite,  # type: ignore[arg-type]
        join_url="http://localhost:3000/join?token=token-abc",
        organization_name="Acme Compliance",
    )
    assert "Hi Carlos," in html
    assert "Master User" in html
    assert "Acme Compliance" in html
    assert "Accept invitation" in html
    assert "Sign in with" in html
    assert "do not need to create a new organization" in html


def test_resolve_member_full_name_falls_back_to_email_local_part() -> None:
    user = CurrentUser(
        user_id="user-new",
        org_id="org_other",
        role="nominated_team_member",
        roles=frozenset({"nominated_team_member"}),
        claims={"email": "sinrepresion@gmail.com"},
    )
    assert (
        invite_service._resolve_member_full_name(user, "sinrepresion@gmail.com")
        == "sinrepresion"
    )


def test_accept_invite_is_idempotent_for_same_member(monkeypatch) -> None:
    invite = SimpleNamespace(
        id="invite-1",
        auth_org_id="org_customer_acme",
        email="sinrepresion@gmail.com",
        invitee_name="Carlos",
        role="master_user",
        token="token-abc",
        status="accepted",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
    )
    member = SimpleNamespace(auth_user_id="user-1", auth_org_id="org_customer_acme")

    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *_a, **_k):
            return self

        def one_or_none(self):
            return self._row

    class _AcceptDB(_DB):
        def __init__(self):
            super().__init__()
            self._calls = 0

        def query(self, model):
            self._calls += 1
            # First query is invite lookup; later is membership check.
            name = getattr(model, "__name__", str(model))
            if "Invite" in name or self._calls == 1:
                return _Query(invite)
            return _Query(member)

    monkeypatch.setattr(invite_service, "update_supabase_active_org", lambda *_a, **_k: True)
    response = invite_service.accept_organization_invite(
        OrganizationInviteAcceptRequest(token="token-abc"),
        _user("org_other", email="sinrepresion@gmail.com"),
        _AcceptDB(),  # type: ignore[arg-type]
    )
    assert response.joined is True
    assert response.auth_org_id == "org_customer_acme"


def test_accept_invite_requires_matching_email(monkeypatch) -> None:
    invite = SimpleNamespace(
        id="invite-1",
        auth_org_id="org_internal_development",
        email="invited@example.com",
        role="master_user",
        token="token-abc",
        status="pending",
        expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
    )

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def one_or_none(self):
            return invite

    class _AcceptDB(_DB):
        def query(self, *_a, **_k):
            return _Query()

    monkeypatch.setattr(invite_service, "upsert_member", lambda *_a, **_k: None)
    monkeypatch.setattr(invite_service, "update_supabase_active_org", lambda *_a, **_k: True)

    with pytest.raises(HTTPException) as exc:
        invite_service.accept_organization_invite(
            OrganizationInviteAcceptRequest(token="token-abc"),
            _user("org_other", email="other@example.com"),
            _AcceptDB(),  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 403
    assert "invited@example.com" in str(exc.value.detail)
    assert "signed in as" in str(exc.value.detail)


def test_list_my_organizations_survives_logo_lookup_errors(monkeypatch) -> None:
    member = SimpleNamespace(
        auth_org_id="org_internal_production",
        role="master_user",
        status="active",
    )
    organization = SimpleNamespace(
        auth_org_id="org_internal_production",
        name="Production",
        kind="internal",
        logo_url="storage://broken/logo.png",
    )

    class _Query:
        def filter(self, *_a, **_k):
            return self

        def all(self):
            return [organization]

    class _ListDB:
        def query(self, *_a, **_k):
            return _Query()

    monkeypatch.setattr(invite_service, "ensure_platform_super_admin_environment_workspace", lambda *_a, **_k: None)
    monkeypatch.setattr(invite_service, "list_memberships_for_user", lambda *_a, **_k: [member])
    monkeypatch.setattr(invite_service, "migrate_legacy_storage_logo_if_needed", lambda *_a, **_k: None)

    def _boom(_logo_url):
        raise RuntimeError("storage down")

    monkeypatch.setattr(invite_service, "public_or_signed_logo_url", _boom)

    listed = invite_service.list_my_organizations(
        _user("org_internal_production"),
        _ListDB(),  # type: ignore[arg-type]
    )
    assert listed.organizations[0].name == "Production"
    assert listed.organizations[0].logo_display_url is None

