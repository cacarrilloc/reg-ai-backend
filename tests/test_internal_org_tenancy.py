"""Internal vs external organization tenancy binding."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.internal_org_service import (
    ORG_KIND_INTERNAL,
    is_internal_organization,
    is_reserved_internal_org_name,
    platform_supabase_project_ref,
    resolve_internal_environment_org,
)
from app.services.organization_tenant_service import (
    TENANT_STATUS_READY,
    bind_organization_to_platform_project,
)
from app.services.supabase_tenant_provisioner import provision_organization_tenant


def test_resolve_internal_org_by_environment() -> None:
    local = resolve_internal_environment_org('local')
    assert local is not None
    assert local.name == 'Development'
    assert local.slug == 'development'
    assert local.expected_project_ref == 'ugnfslbacesddjiyjhvp'

    staging = resolve_internal_environment_org('staging')
    assert staging is not None
    assert staging.name == 'Staging'

    production = resolve_internal_environment_org('production')
    assert production is not None
    assert production.name == 'Production'
    assert production.expected_project_ref == 'rednxostzguusaagguzo'


def test_reserved_internal_names() -> None:
    assert is_reserved_internal_org_name('Development')
    assert is_reserved_internal_org_name('staging')
    assert not is_reserved_internal_org_name('Acme Corp')


def test_platform_super_admin_email(monkeypatch) -> None:
    from app.services.internal_org_service import (
        is_platform_super_admin_email,
        master_account_email,
    )

    settings = MagicMock()
    settings.master_account = 'haider.mumtaz@ipxenterprises.com'
    monkeypatch.setattr(
        'app.services.internal_org_service.get_settings',
        lambda: settings,
    )

    assert master_account_email() == 'haider.mumtaz@ipxenterprises.com'
    assert is_platform_super_admin_email('haider.mumtaz@ipxenterprises.com')
    assert is_platform_super_admin_email('Haider.Mumtaz@IPXEnterprises.com')
    assert not is_platform_super_admin_email('someone@example.com')
    assert not is_platform_super_admin_email(None)

    settings.master_account = 'new.owner@example.com'
    assert is_platform_super_admin_email('new.owner@example.com')
    assert not is_platform_super_admin_email('haider.mumtaz@ipxenterprises.com')


def test_bootstrap_uuid_organization_detection() -> None:
    from app.services.internal_org_service import (
        is_bootstrap_uuid_organization,
        is_uuid_string,
    )

    assert is_uuid_string('dce4ef74-5e19-4309-9f42-fb3b710c07fa')
    assert not is_uuid_string('org_internal_production')

    uuid_org = MagicMock()
    uuid_org.auth_org_id = 'dce4ef74-5e19-4309-9f42-fb3b710c07fa'
    uuid_org.name = 'dce4ef74-5e19-4309-9f42-fb3b710c07fa'
    uuid_org.kind = 'external'
    assert is_bootstrap_uuid_organization(uuid_org)

    named = MagicMock()
    named.auth_org_id = 'dce4ef74-5e19-4309-9f42-fb3b710c07fa'
    named.name = 'Acme Corp'
    named.kind = 'external'
    assert not is_bootstrap_uuid_organization(named)

    internal = MagicMock()
    internal.auth_org_id = 'org_internal_production'
    internal.name = 'Production'
    internal.kind = 'internal'
    assert not is_bootstrap_uuid_organization(internal)


def test_super_admin_login_ensures_environment_org(monkeypatch) -> None:
    from app.core.roles import MASTER_USER
    from app.core.security import CurrentUser
    from app.services import organization_invite_service as invite_service

    seeded = MagicMock()
    seeded.auth_org_id = 'org_internal_production'
    seeded.name = 'Production'
    seeded.slug = 'production'
    seeded.kind = ORG_KIND_INTERNAL

    uuid_org_id = 'dce4ef74-5e19-4309-9f42-fb3b710c07fa'
    uuid_org = MagicMock()
    uuid_org.auth_org_id = uuid_org_id
    uuid_org.name = uuid_org_id
    uuid_org.kind = 'external'

    uuid_member = MagicMock()
    uuid_member.auth_org_id = uuid_org_id
    uuid_member.status = 'active'

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        invite_service,
        'is_platform_super_admin_email',
        lambda email: True,
    )
    monkeypatch.setattr(
        invite_service,
        '_claim_email',
        lambda user: 'haider.mumtaz@ipxenterprises.com',
    )
    monkeypatch.setattr(
        invite_service,
        '_profile_email',
        lambda user, db: None,
    )
    monkeypatch.setattr(
        'app.services.internal_org_service.seed_internal_environment_organization',
        lambda db, settings=None: seeded,
    )

    def fake_upsert(db, **kwargs):
        calls['upsert'] = kwargs
        return MagicMock()

    monkeypatch.setattr(invite_service, 'upsert_member', fake_upsert)

    def fake_mark_failed(db, *, auth_org_id, error, supabase_project_ref=None):
        calls['failed'] = auth_org_id
        return MagicMock()

    monkeypatch.setattr(
        'app.services.organization_tenant_service.mark_tenant_failed',
        fake_mark_failed,
    )

    def fake_update_active(user_id, auth_org_id, settings, *, role=None):
        calls['active'] = (auth_org_id, role)
        return True

    monkeypatch.setattr(invite_service, 'update_supabase_active_org', fake_update_active)

    membership_query = MagicMock()
    membership_query.filter.return_value.all.return_value = [uuid_member]

    org_query = MagicMock()
    org_query.filter.return_value.one_or_none.return_value = uuid_org

    db = MagicMock()

    def query_side_effect(model):
        name = getattr(model, '__name__', str(model))
        if 'OrganizationMember' in name or model is invite_service.OrganizationMember:
            return membership_query
        return org_query

    db.query.side_effect = query_side_effect

    user = CurrentUser(
        user_id='user_haider',
        org_id=uuid_org_id,
        role=MASTER_USER,
        claims={'email': 'haider.mumtaz@ipxenterprises.com'},
    )

    org = invite_service.ensure_platform_super_admin_environment_workspace(user, db)

    assert org is seeded
    assert calls['upsert']['auth_org_id'] == 'org_internal_production'
    assert calls['upsert']['role'] == MASTER_USER
    assert calls['failed'] == uuid_org_id
    assert uuid_member.status == 'removed'
    assert calls['active'] == ('org_internal_production', MASTER_USER)
    db.commit.assert_called()


def test_environment_organization_name() -> None:
    from app.services.internal_org_service import environment_organization_name

    assert environment_organization_name(MagicMock(environment='production')) == 'Production'
    assert environment_organization_name(MagicMock(environment='local')) == 'Development'
    assert environment_organization_name(MagicMock(environment='staging')) == 'Staging'


def test_super_admin_create_reserved_name_joins_seeded_internal(monkeypatch) -> None:
    from app.core.roles import MASTER_USER
    from app.services import org_lifecycle_service as lifecycle

    seeded = MagicMock()
    seeded.auth_org_id = 'org_internal_development'
    seeded.name = 'Development'
    seeded.slug = 'development'
    seeded.kind = ORG_KIND_INTERNAL
    seeded.id = 'org-row-1'

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        'app.services.internal_org_service.seed_internal_environment_organization',
        lambda db, settings=None: seeded,
    )

    def fake_upsert(db, **kwargs):
        calls['upsert'] = kwargs
        return MagicMock()

    monkeypatch.setattr(lifecycle, 'upsert_member', fake_upsert)

    db = MagicMock()
    org = lifecycle.create_organization(
        db,
        name='Development',
        creator_user_id='user_admin',
        creator_email='haider.mumtaz@ipxenterprises.com',
        creator_name='Haider',
    )

    assert org is seeded
    assert calls['upsert']['auth_org_id'] == 'org_internal_development'
    assert calls['upsert']['role'] == MASTER_USER
    db.commit.assert_called_once()


def test_non_admin_create_reserved_name_still_blocked() -> None:
    from app.services import org_lifecycle_service as lifecycle

    db = MagicMock()
    try:
        lifecycle.create_organization(
            db,
            name='Development',
            creator_user_id='user_other',
            creator_email='someone@example.com',
        )
        raised = False
    except ValueError as exc:
        raised = True
        assert 'reserved internal accounts' in str(exc)
    assert raised is True


def test_customer_org_creator_is_master_user(monkeypatch) -> None:
    from app.core.roles import MASTER_USER
    from app.services import org_lifecycle_service as lifecycle

    upserted: dict[str, object] = {}

    def fake_get_or_create(db, **kwargs):
        org = MagicMock()
        org.auth_org_id = kwargs['auth_org_id']
        org.name = kwargs['name']
        return org

    def fake_upsert(db, **kwargs):
        upserted.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(lifecycle, 'get_or_create_organization', fake_get_or_create)
    monkeypatch.setattr(lifecycle, 'upsert_member', fake_upsert)
    monkeypatch.setattr(lifecycle, 'flush_pending_tenant_provisions', lambda: None)

    db = MagicMock()
    lifecycle.create_organization(
        db,
        name='Acme Compliance',
        creator_user_id='user_founder',
        creator_email='founder@firm.com',
        creator_name='Ada',
    )

    assert upserted['auth_user_id'] == 'user_founder'
    assert upserted['role'] == MASTER_USER
    assert upserted['email'] == 'founder@firm.com'
    db.commit.assert_called_once()


def test_platform_project_ref_from_url() -> None:
    settings = MagicMock()
    settings.supabase_url = 'https://ugnfslbacesddjiyjhvp.supabase.co'
    assert platform_supabase_project_ref(settings) == 'ugnfslbacesddjiyjhvp'


def test_bind_organization_to_platform_project(monkeypatch) -> None:
    settings = MagicMock()
    settings.database_url = 'postgresql+psycopg://user:pass@db/postgres'
    settings.direct_url = None
    settings.supabase_url = 'https://ugnfslbacesddjiyjhvp.supabase.co'
    settings.supabase_service_role_key = 'service-role'
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    db = MagicMock()

    monkeypatch.setattr(
        'app.services.organization_tenant_service.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.organization_tenant_service.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.organization_tenant_service.clear_tenant_binding_cache',
        lambda *args, **kwargs: None,
        raising=False,
    )

    ready = bind_organization_to_platform_project(
        db,
        auth_org_id='org_internal_development',
        settings=settings,
        expected_project_ref='ugnfslbacesddjiyjhvp',
    )
    assert ready is tenant
    assert tenant.status == TENANT_STATUS_READY
    assert tenant.supabase_project_ref == 'ugnfslbacesddjiyjhvp'
    assert tenant.database_url == settings.database_url
    assert tenant.supabase_service_role_key == 'service-role'


def test_provision_skips_internal_org(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.database_url = 'postgresql+psycopg://user:pass@db/postgres'
    settings.direct_url = None
    settings.supabase_url = 'https://ugnfslbacesddjiyjhvp.supabase.co'
    settings.supabase_service_role_key = 'service-role'
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.database_url = None
    tenant.supabase_project_ref = None

    organization = MagicMock()
    organization.kind = ORG_KIND_INTERNAL
    organization.auth_org_id = 'org_internal_development'
    organization.slug = 'development'

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = organization

    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )

    bound = MagicMock()
    bound.status = TENANT_STATUS_READY
    bound.supabase_project_ref = 'ugnfslbacesddjiyjhvp'
    monkeypatch.setattr(
        'app.services.organization_tenant_service.bind_organization_to_platform_project',
        lambda *args, **kwargs: bound,
    )

    result = provision_organization_tenant(
        db, auth_org_id='org_internal_development', settings=settings
    )
    assert result['skipped'] is True
    assert result['reason'] == 'internal_org_uses_platform_project'
    assert is_internal_organization(organization)
