"""Per-organization Postgres schema isolation on the shared Supabase project."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.organization_tenant_service import (
    ISOLATION_SHARED,
    TENANT_STATUS_READY,
    bind_organization_to_platform_project,
)
from app.services.schema_tenant_provisioner import (
    DATA_PLANE_TABLES,
    SIDEBAR_DATA_PLANE_FEATURES,
    TENANT_SCHEMA_RE,
    provision_organization_schema_tenant,
    tenant_schema_name,
)


def test_tenant_schema_name_is_stable_and_valid() -> None:
    assert tenant_schema_name('carrilca-test-org') == 'carrilca_test_org_tenant'
    assert tenant_schema_name('Acme Compliance!') == 'acmecompliance_tenant'
    assert tenant_schema_name('Acme___Corp') == 'acme_corp_tenant'
    assert tenant_schema_name('TENANT_already') == 'already_tenant'
    assert TENANT_SCHEMA_RE.fullmatch(tenant_schema_name('carrilca-test-org'))
    assert tenant_schema_name('carrilca_test_org_tenant') == 'carrilca_test_org_tenant'
    assert TENANT_SCHEMA_RE.fullmatch('tenant_carrilca_test_org')
    assert TENANT_SCHEMA_RE.fullmatch('carrilca_test_org_tenant')


def test_tenant_schema_name_omits_reserved_characters() -> None:
    assert tenant_schema_name("Org / Name (prod)") == 'orgnameprod_tenant'
    assert tenant_schema_name('  hello--world  ') == 'hello_world_tenant'


def test_tenant_schema_name_rejects_empty() -> None:
    with pytest.raises(ValueError):
        tenant_schema_name('')
    with pytest.raises(ValueError):
        tenant_schema_name('   ')
    with pytest.raises(ValueError):
        tenant_schema_name('!!!')


def test_allocate_tenant_schema_name_appends_suffix_when_taken(monkeypatch) -> None:
    from app.services import schema_tenant_provisioner as provisioner

    monkeypatch.setattr(
        provisioner,
        '_schema_name_taken',
        lambda db, name, auth_org_id: name == 'acme_tenant',
    )
    allocated = provisioner.allocate_tenant_schema_name(
        MagicMock(),
        organization_name='Acme',
        auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    )
    assert allocated == 'acme_aaaaaaaa_tenant'


def test_rewrite_cloned_fk_qualifies_tenant_and_public_targets() -> None:
    from app.services.schema_tenant_provisioner import rewrite_cloned_fk_definition

    policy_fk = rewrite_cloned_fk_definition(
        'FOREIGN KEY (policy_id) REFERENCES policies(id) ON DELETE CASCADE',
        referenced_table='policies',
        tenant_schema='carrilca_test_org_tenant',
    )
    assert (
        policy_fk
        == 'FOREIGN KEY (policy_id) REFERENCES "carrilca_test_org_tenant"."policies"(id) ON DELETE CASCADE'
    )

    publication_fk = rewrite_cloned_fk_definition(
        'FOREIGN KEY (publication_id) REFERENCES regulatory_publications(id) ON DELETE CASCADE',
        referenced_table='regulatory_publications',
        tenant_schema='carrilca_test_org_tenant',
    )
    assert (
        publication_fk
        == 'FOREIGN KEY (publication_id) REFERENCES "public"."regulatory_publications"(id) ON DELETE CASCADE'
    )


def test_data_plane_tables_exclude_control_plane() -> None:
    assert 'organizations' not in DATA_PLANE_TABLES
    assert 'organization_members' not in DATA_PLANE_TABLES
    assert 'organization_tenants' not in DATA_PLANE_TABLES
    assert 'regulatory_publications' not in DATA_PLANE_TABLES
    assert 'general_newsletter_subscribers' not in DATA_PLANE_TABLES
    assert 'newsletter_subscribers' in DATA_PLANE_TABLES
    assert 'usage_counters' in DATA_PLANE_TABLES
    assert 'policies' in DATA_PLANE_TABLES
    assert 'regulations' in DATA_PLANE_TABLES
    assert 'gap_analyses' in DATA_PLANE_TABLES
    assert 'policy_revisions' in DATA_PLANE_TABLES
    assert 'marketing_contents' in DATA_PLANE_TABLES
    assert 'business_changes' in DATA_PLANE_TABLES
    assert 'audit_logs' in DATA_PLANE_TABLES
    assert 'support_tickets' in DATA_PLANE_TABLES
    for tables in SIDEBAR_DATA_PLANE_FEATURES.values():
        assert set(tables).issubset(DATA_PLANE_TABLES)


def test_master_account_create_shares_platform_schema(monkeypatch) -> None:
    from app.services import org_lifecycle_service as lifecycle

    created: dict[str, object] = {}

    def fake_get_or_create(db, **kwargs):
        created.update(kwargs)
        org = MagicMock()
        org.auth_org_id = kwargs['auth_org_id']
        org.name = kwargs['name']
        return org

    monkeypatch.setattr(lifecycle, 'get_or_create_organization', fake_get_or_create)
    monkeypatch.setattr(lifecycle, 'upsert_member', lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(lifecycle, 'flush_pending_tenant_provisions', lambda: None)

    db = MagicMock()
    lifecycle.create_organization(
        db,
        name='Acme Compliance',
        creator_user_id='user_master',
        creator_email='haider.mumtaz@ipxenterprises.com',
    )
    assert created['isolate_database'] is False


def test_customer_org_queues_schema_when_provisioning_configured(monkeypatch) -> None:
    from app.services import org_lifecycle_service as lifecycle

    queued: list[str] = []
    settings = MagicMock()
    settings.tenant_provisioning_configured = True

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    nested = MagicMock()
    nested.__enter__.return_value = None
    nested.__exit__.return_value = False
    db.begin_nested.return_value = nested

    monkeypatch.setattr(lifecycle, '_queue_tenant_provision', queued.append)
    monkeypatch.setattr('app.core.config.get_settings', lambda: settings)
    monkeypatch.setattr(
        'app.services.organization_tenant_service.ensure_organization_tenant',
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        'app.services.entitlement_service.seed_default_entitlements',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.role_permissions_service.ensure_organization_role_permissions',
        lambda *args, **kwargs: None,
    )

    def should_not_bind(*args, **kwargs):
        raise AssertionError('customer orgs must not bind to the shared schema')

    monkeypatch.setattr(
        'app.services.organization_tenant_service.bind_organization_to_platform_project',
        should_not_bind,
    )

    lifecycle.get_or_create_organization(
        db,
        auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        name='Acme Compliance',
        isolate_database=True,
    )
    assert queued == ['aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee']


def test_customer_org_create_requires_isolation_when_provisioning_disabled(monkeypatch) -> None:
    from app.services import org_lifecycle_service as lifecycle

    queued: list[str] = []
    settings = MagicMock()
    settings.tenant_provisioning_configured = False

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    nested = MagicMock()
    nested.__enter__.return_value = None
    nested.__exit__.return_value = False
    db.begin_nested.return_value = nested

    monkeypatch.setattr(lifecycle, '_queue_tenant_provision', queued.append)
    monkeypatch.setattr('app.core.config.get_settings', lambda: settings)
    monkeypatch.setattr(
        'app.services.organization_tenant_service.ensure_organization_tenant',
        lambda *args, **kwargs: MagicMock(),
    )
    monkeypatch.setattr(
        'app.services.entitlement_service.seed_default_entitlements',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.role_permissions_service.ensure_organization_role_permissions',
        lambda *args, **kwargs: None,
    )

    def should_not_bind(*args, **kwargs):
        raise AssertionError('customer orgs must not bind to the shared schema')

    monkeypatch.setattr(
        'app.services.organization_tenant_service.bind_organization_to_platform_project',
        should_not_bind,
    )

    with pytest.raises(ValueError, match='dedicated database schema'):
        lifecycle.get_or_create_organization(
            db,
            auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            name='Acme Compliance',
            isolate_database=True,
        )
    assert queued == []


def test_customer_create_requests_isolated_schema(monkeypatch) -> None:
    from app.services import org_lifecycle_service as lifecycle

    created: dict[str, object] = {}

    def fake_get_or_create(db, **kwargs):
        created.update(kwargs)
        org = MagicMock()
        org.auth_org_id = kwargs['auth_org_id']
        org.name = kwargs['name']
        return org

    monkeypatch.setattr(lifecycle, 'get_or_create_organization', fake_get_or_create)
    monkeypatch.setattr(lifecycle, 'upsert_member', lambda *args, **kwargs: MagicMock())
    monkeypatch.setattr(lifecycle, 'flush_pending_tenant_provisions', lambda: None)

    db = MagicMock()
    lifecycle.create_organization(
        db,
        name='Acme Compliance',
        creator_user_id='user_cco',
        creator_email='cco@firm.com',
    )
    assert created['isolate_database'] is True


def test_bind_platform_marks_shared_public_schema(monkeypatch) -> None:
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
    assert tenant.isolation_mode == ISOLATION_SHARED
    assert tenant.schema_name == 'public'


def test_schema_provision_skipped_when_not_configured(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = False
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.schema_name = None
    tenant.database_url = None

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_failed',
        lambda *args, **kwargs: tenant,
    )

    result = provision_organization_schema_tenant(
        db, auth_org_id='org_test', settings=settings
    )
    assert result['status'] == 'failed'
    assert result['reason'] == 'provisioning_not_configured'


def test_schema_provision_creates_schema_and_marks_ready(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.is_production = True
    settings.database_url = 'postgresql+psycopg://user:pass@db/postgres'
    settings.direct_url = None
    settings.supabase_url = 'https://example.supabase.co'
    settings.supabase_service_role_key = 'service-role'
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.schema_name = None
    tenant.database_url = None

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_provisioning',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.create_organization_schema',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.copy_organization_data_into_schema',
        lambda *args, **kwargs: {},
    )
    seeded: list[str] = []
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner._seed_isolated_org_catalog',
        lambda auth_org_id: seeded.append(auth_org_id),
    )

    ready_row = MagicMock()
    ready_row.status = TENANT_STATUS_READY
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_ready',
        lambda *args, **kwargs: ready_row,
    )

    result = provision_organization_schema_tenant(
        db,
        auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        org_name='carrilca-test-org',
        settings=settings,
    )
    assert result['status'] == 'ready'
    assert result['schema_name'] == 'carrilca_test_org_tenant'
    assert result['skipped'] is False
    assert seeded == ['aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee']


def test_resolve_schema_for_ready_tenant(monkeypatch) -> None:
    from app.db import session as session_mod

    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(
            database_url='postgresql+psycopg://platform',
            tenant_db_routing_enabled=True,
        ),
    )
    monkeypatch.setattr(
        session_mod,
        '_lookup_ready_tenant_binding',
        lambda org_id: (
            'postgresql+psycopg://platform',
            'https://example.supabase.co',
            'service-role',
            'tenant_acme',
        ),
    )
    session_mod.clear_tenant_binding_cache()
    assert session_mod.resolve_schema_for_org('org_ready') == 'tenant_acme'
    assert session_mod.resolve_schema_for_org('org_ready', force_platform=True) is None


def test_schema_mode_is_configured_with_database_url_only() -> None:
    from app.core.config import Settings

    settings = Settings.model_construct(
        environment='production',
        tenant_provisioning_enabled=True,
        tenant_isolation_mode='schema',
        database_url='postgresql+psycopg://user:pass@db/postgres',
        supabase_access_token=None,
        supabase_platform_org_slug=None,
    )
    assert settings.is_production is True
    assert settings.tenant_provisioning_configured is True

    project_settings = Settings.model_construct(
        environment='production',
        tenant_provisioning_enabled=True,
        tenant_isolation_mode='project',
        database_url='postgresql+psycopg://user:pass@db/postgres',
        supabase_access_token=None,
        supabase_platform_org_slug=None,
    )
    assert project_settings.tenant_provisioning_configured is False


def test_schema_mode_is_configured_on_dev_and_staging() -> None:
    from app.core.config import Settings

    for environment in ('local', 'dev', 'development', 'staging', 'production'):
        settings = Settings.model_construct(
            environment=environment,
            tenant_provisioning_enabled=True,
            tenant_isolation_mode='schema',
            database_url='postgresql+psycopg://user:pass@db/postgres',
        )
        assert settings.tenant_provisioning_configured is True


def test_schema_isolation_disabled_when_flag_off() -> None:
    from app.core.config import Settings

    for environment in ('local', 'dev', 'staging', 'production'):
        settings = Settings.model_construct(
            environment=environment,
            tenant_provisioning_enabled=False,
            tenant_isolation_mode='schema',
            database_url='postgresql+psycopg://user:pass@db/postgres',
        )
        assert settings.tenant_provisioning_configured is False


def test_schema_provision_creates_schema_outside_production(monkeypatch) -> None:
    settings = MagicMock()
    settings.is_production = False
    settings.tenant_provisioning_configured = True
    settings.frontend_url = 'http://localhost:3000'
    settings.database_url = 'postgresql+psycopg://user:pass@db/postgres'
    settings.direct_url = None
    settings.supabase_url = 'https://example.supabase.co'
    settings.supabase_service_role_key = 'service-role'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.schema_name = None
    tenant.database_url = None

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_provisioning',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.create_organization_schema',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.copy_organization_data_into_schema',
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner._seed_isolated_org_catalog',
        lambda auth_org_id: None,
    )

    ready_row = MagicMock()
    ready_row.status = TENANT_STATUS_READY
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_ready',
        lambda *args, **kwargs: ready_row,
    )

    result = provision_organization_schema_tenant(
        db,
        auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        org_name='carrilca-test-org',
        settings=settings,
    )
    assert result['status'] == 'ready'
    assert result['schema_name'] == 'carrilca_test_org_tenant'
    assert result['skipped'] is False


def test_drop_schema_refuses_public() -> None:
    from app.services.schema_tenant_provisioner import drop_organization_schema

    with pytest.raises(ValueError):
        drop_organization_schema('public')
    with pytest.raises(ValueError):
        drop_organization_schema('pg_catalog')


def test_bind_data_plane_session_sets_tenant_schema(monkeypatch) -> None:
    from app.db import session as session_mod

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}

    monkeypatch.setattr(
        session_mod,
        'resolve_schema_for_org',
        lambda org_id, force_platform=False: (
            'carrilca_test_org_tenant' if org_id == 'org_tenant' else None
        ),
    )
    db = FakeSession()
    session_mod.bind_data_plane_session(db, org_id='org_tenant')
    assert db.info['tenant_schema'] == 'carrilca_test_org_tenant'

    shared = FakeSession()
    session_mod.bind_data_plane_session(shared, org_id='org_shared')
    assert 'tenant_schema' not in shared.info


def test_bind_data_plane_session_skips_test_doubles() -> None:
    from app.db.session import bind_data_plane_session

    class RecordingSession:
        pass

    db = RecordingSession()
    assert bind_data_plane_session(db, org_id='org_tenant') is db


def test_analysis_session_scope_binds_tenant_schema(monkeypatch) -> None:
    from app.db import session as session_mod
    from app.services import gap_analysis_service as gap_service

    class FakeSession:
        def __init__(self) -> None:
            self.info: dict[str, object] = {}
            self.closed = False

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    fake = FakeSession()
    monkeypatch.setattr(gap_service, 'SessionLocal', lambda: fake)
    monkeypatch.setattr(
        session_mod,
        'resolve_schema_for_org',
        lambda org_id, force_platform=False: (
            'carrilca_test_org_tenant' if org_id == 'org_tenant' else None
        ),
    )
    token = session_mod.set_tenant_org_id('org_tenant')
    try:
        with gap_service._analysis_session_scope(commit=False) as db:
            assert db.info['tenant_schema'] == 'carrilca_test_org_tenant'
    finally:
        session_mod.reset_tenant_org_id(token)
    assert fake.closed is True


def test_prepare_org_data_plane_allows_shared_orgs(monkeypatch) -> None:
    from app.db import session as session_mod

    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(tenant_db_routing_enabled=True),
    )
    monkeypatch.setattr(
        session_mod,
        '_lookup_tenant_status',
        lambda org_id: ('shared', 'ready'),
    )
    session_mod.prepare_org_data_plane('org_legacy_shared')


def test_prepare_org_data_plane_skips_internal_org(monkeypatch) -> None:
    from app.db import session as session_mod

    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(tenant_db_routing_enabled=True),
    )

    def should_not_lookup(org_id: str) -> None:
        raise AssertionError(f'internal org should not query tenant status: {org_id}')

    monkeypatch.setattr(session_mod, '_lookup_tenant_status', should_not_lookup)
    session_mod.prepare_org_data_plane('org_internal_production')


def test_prepare_org_data_plane_blocks_pending_schema_tenant(monkeypatch) -> None:
    from app.db import session as session_mod

    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(tenant_db_routing_enabled=True),
    )
    monkeypatch.setattr(
        session_mod,
        '_lookup_tenant_status',
        lambda org_id: ('schema', 'pending'),
    )
    monkeypatch.setattr(
        'app.services.tenant_provision_dispatch.schedule_organization_tenant_provision',
        lambda org_id: None,
    )
    with pytest.raises(session_mod.TenantSchemaNotReady, match='still being isolated'):
        session_mod.prepare_org_data_plane('org_customer')


def test_prepare_org_data_plane_ready_after_inline_provision(monkeypatch) -> None:
    from app.db import session as session_mod

    states = iter([('schema', 'pending'), ('schema', 'ready')])
    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(tenant_db_routing_enabled=True),
    )
    monkeypatch.setattr(
        session_mod,
        '_lookup_tenant_status',
        lambda org_id: next(states),
    )
    monkeypatch.setattr(
        'app.services.tenant_provision_dispatch.schedule_organization_tenant_provision',
        lambda org_id: None,
    )
    session_mod.prepare_org_data_plane('org_customer')


def test_schema_provision_copies_org_owned_rows(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.database_url = 'postgresql+psycopg://user:pass@db/postgres'
    settings.direct_url = None
    settings.supabase_url = 'https://example.supabase.co'
    settings.supabase_service_role_key = 'service-role'
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.schema_name = None
    tenant.database_url = None
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    copied: list[tuple[str, str]] = []

    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_provisioning',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.create_organization_schema',
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.copy_organization_data_into_schema',
        lambda schema_name, auth_org_id, **kwargs: copied.append((schema_name, auth_org_id)) or {},
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner._seed_isolated_org_catalog',
        lambda auth_org_id: None,
    )
    monkeypatch.setattr(
        'app.services.schema_tenant_provisioner.mark_tenant_ready',
        lambda *args, **kwargs: MagicMock(status=TENANT_STATUS_READY),
    )

    provision_organization_schema_tenant(
        db,
        auth_org_id='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        org_name='carrilca-test-org',
        settings=settings,
    )
    assert copied == [
        ('carrilca_test_org_tenant', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
    ]


def test_database_usernames_for_grants_reads_pooler_and_direct() -> None:
    from app.core.config import Settings
    from app.services.schema_tenant_provisioner import database_usernames_for_grants

    settings = Settings.model_construct(
        database_url='postgresql+psycopg://pooler:pass@db:6543/postgres',
        direct_url='postgresql+psycopg://postgres:pass@db:5432/postgres',
    )
    assert database_usernames_for_grants(settings) == ('pooler', 'postgres')


def test_use_shared_search_path_only_when_tenant_schema_bound() -> None:
    from app.db.session import use_shared_search_path

    class FakeSession:
        def __init__(self, info: dict[str, object]) -> None:
            self.info = info
            self.statements: list[object] = []

        def execute(self, statement: object) -> None:
            self.statements.append(statement)

    tenant = FakeSession({'tenant_schema': 'acme_tenant'})
    use_shared_search_path(tenant)
    assert len(tenant.statements) == 1
    assert 'SET LOCAL search_path TO public' in str(tenant.statements[0])

    shared = FakeSession({})
    use_shared_search_path(shared)
    assert shared.statements == []


def test_grant_tenant_schema_privileges_targets_quoted_role() -> None:
    from app.services.schema_tenant_provisioner import grant_tenant_schema_privileges

    statements: list[str] = []

    class FakeConn:
        def execute(self, sql: str) -> None:
            statements.append(sql)

    grant_tenant_schema_privileges(FakeConn(), 'acme_tenant', ('pooler',))
    assert any('GRANT USAGE, CREATE ON SCHEMA "acme_tenant" TO "pooler"' in sql for sql in statements)
    assert any('ALL TABLES IN SCHEMA "acme_tenant"' in sql for sql in statements)


def test_record_usage_opens_tenant_session(monkeypatch) -> None:
    from app.services import usage_service as usage

    opened: list[str | None] = []

    class _Scope:
        def __enter__(self):
            return MagicMock()

        def __exit__(self, *_args):
            return False

    def fake_session_scope(*, commit: bool = True, org_id: str | None = None):
        opened.append(org_id)
        return _Scope()

    monkeypatch.setattr(
        'app.db.session.session_scope',
        fake_session_scope,
    )
    monkeypatch.setattr(usage, 'increment_usage', lambda *args, **kwargs: None)
    usage.record_usage('org_customer', 'policy_upload')
    assert opened == ['org_customer']
