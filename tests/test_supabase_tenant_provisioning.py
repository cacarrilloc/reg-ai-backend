"""Tests for per-org Supabase project provisioning and DB routing."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx

from app.services.organization_tenant_service import (
    TENANT_STATUS_FAILED,
    TENANT_STATUS_READY,
    ensure_organization_tenant,
    mark_tenant_ready,
)
from app.services.supabase_tenant_provisioner import (
    _build_database_url,
    _project_name,
    provision_organization_tenant,
)
from app.services.tenant_migrations import (
    TENANT_MIGRATION_FILES,
    is_platform_only_migration,
    tenant_migration_paths,
)


def test_tenant_migration_bundle_excludes_platform_only() -> None:
    assert not any(name.startswith('phase_23_') for name in TENANT_MIGRATION_FILES)
    assert not any(name.startswith('phase_24_') for name in TENANT_MIGRATION_FILES)
    assert is_platform_only_migration('phase_23_organization_tenants.sql')
    assert is_platform_only_migration('phase_24_organization_tenant_credentials.sql')
    paths = tenant_migration_paths()
    assert paths
    assert all(path.is_file() for path in paths)
    names = {path.name for path in paths}
    assert 'tenant_phase_40_marketing_review_agent.sql' in names
    assert 'tenant_phase_41_marketing_status_checks.sql' in names
    assert 'tenant_phase_44_newsletter_subscribers.sql' in names
    assert 'phase_44_newsletter_subscribers.sql' not in names


def test_project_name_and_database_url_helpers() -> None:
    assert _project_name('org_abcdefghijkl').startswith('reg-ai-')
    url = _build_database_url(project_ref='abc123xyz', db_password='s3cret!')
    assert url.startswith('postgresql+psycopg://postgres:')
    assert '@db.abc123xyz.supabase.co:5432/postgres' in url


def test_provision_skipped_when_not_configured(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = False
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.database_url = None
    tenant.supabase_project_ref = None

    db = MagicMock()

    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.mark_tenant_failed',
        lambda *args, **kwargs: tenant,
    )

    result = provision_organization_tenant(
        db, auth_org_id='org_test', settings=settings
    )

    assert result['status'] == 'failed'
    assert result['reason'] == 'provisioning_not_configured'
    db.commit.assert_called()


def test_provision_noop_when_already_ready(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True

    tenant = MagicMock()
    tenant.status = TENANT_STATUS_READY
    tenant.database_url = 'postgresql+psycopg://x'
    tenant.supabase_project_ref = 'readyref'

    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )

    result = provision_organization_tenant(
        MagicMock(), auth_org_id='org_test', settings=settings
    )
    assert result['status'] == 'ready'
    assert result['skipped'] is True


def test_provision_marks_failed_on_api_error(monkeypatch) -> None:
    settings = MagicMock()
    settings.tenant_provisioning_configured = True
    settings.tenant_isolation_mode = 'project'
    settings.supabase_access_token = 'sbp_test'
    settings.supabase_platform_org_slug = 'my-org'
    settings.supabase_tenant_region = 'us-east-1'
    settings.supabase_tenant_db_instance_size = 'micro'
    settings.frontend_url = 'http://localhost:3000'

    tenant = MagicMock()
    tenant.status = 'pending'
    tenant.database_url = None
    tenant.supabase_project_ref = None

    failed = MagicMock()
    failed.status = TENANT_STATUS_FAILED

    def fake_post(*args, **kwargs):
        del args, kwargs
        return httpx.Response(403, text='forbidden', request=httpx.Request('POST', 'https://x'))

    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.mark_tenant_provisioning',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.mark_tenant_failed',
        lambda *args, **kwargs: failed,
    )
    monkeypatch.setattr(
        'app.services.supabase_tenant_provisioner.httpx.post',
        fake_post,
    )

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None

    result = provision_organization_tenant(db, auth_org_id='org_test', settings=settings)
    assert result['status'] == 'failed'
    assert '403' in (result.get('error') or '')


def test_lookup_ready_tenant_binding_does_not_cache_misses(monkeypatch) -> None:
    from app.db import session as session_mod

    queries = {'count': 0}

    class _Query:
        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> None:
            queries['count'] += 1
            return None

    class _Session:
        def query(self, *_args: object, **_kwargs: object) -> _Query:
            return _Query()

        def close(self) -> None:
            return None

    monkeypatch.setattr(session_mod, 'SessionLocal', lambda: _Session())
    session_mod.clear_tenant_binding_cache()
    try:
        assert session_mod._lookup_ready_tenant_binding('org_none') is None
        assert session_mod._lookup_ready_tenant_binding('org_none') is None
        assert queries['count'] == 2
    finally:
        session_mod.clear_tenant_binding_cache()


def test_resolve_database_url_falls_back_to_platform(monkeypatch) -> None:
    from app.db import session as session_mod

    monkeypatch.setattr(session_mod, 'SessionLocal', None)
    monkeypatch.setattr(
        session_mod,
        'get_settings',
        lambda: MagicMock(
            database_url='postgresql+psycopg://platform',
            tenant_db_routing_enabled=True,
        ),
    )
    session_mod.clear_tenant_binding_cache()
    assert (
        session_mod.resolve_database_url_for_org('org_x')
        == 'postgresql+psycopg://platform'
    )


def test_resolve_database_url_uses_ready_tenant(monkeypatch) -> None:
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
            'postgresql+psycopg://tenant',
            'https://tenant.supabase.co',
            'service-role',
            None,
        ),
    )
    session_mod.clear_tenant_binding_cache()
    assert (
        session_mod.resolve_database_url_for_org('org_ready')
        == 'postgresql+psycopg://tenant'
    )


def test_schedule_schema_provision_does_not_block_running_loop(monkeypatch) -> None:
    from app.services import tenant_provision_dispatch as dispatch

    @contextmanager
    def fake_platform_scope(*, commit: bool = True):
        del commit
        yield MagicMock()

    tenant = MagicMock()
    tenant.status = 'pending'
    provisioned: list[str] = []

    monkeypatch.setattr(dispatch, 'platform_session_scope', fake_platform_scope)
    monkeypatch.setattr(
        dispatch,
        'ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        dispatch,
        'get_settings',
        lambda: MagicMock(
            tenant_provisioning_configured=True,
            inngest_async_dispatch_enabled=False,
            tenant_isolation_mode='schema',
        ),
    )
    monkeypatch.setattr(
        dispatch,
        '_provision_tenant_now',
        lambda auth_org_id: provisioned.append(auth_org_id),
    )

    async def _schedule() -> None:
        dispatch.schedule_organization_tenant_provision('org_new')
        assert provisioned == []

    import asyncio

    asyncio.run(_schedule())
    assert provisioned == []


def test_schedule_organization_tenant_provision_queues(monkeypatch) -> None:
    from app.services import tenant_provision_dispatch as dispatch

    @contextmanager
    def fake_platform_scope(*, commit: bool = True):
        del commit
        yield MagicMock()

    tenant = MagicMock()
    tenant.status = 'pending'

    monkeypatch.setattr(dispatch, 'platform_session_scope', fake_platform_scope)
    monkeypatch.setattr(
        dispatch,
        'ensure_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        dispatch,
        'get_settings',
        lambda: MagicMock(
            tenant_provisioning_configured=False,
            inngest_async_dispatch_enabled=False,
        ),
    )

    # No running loop → sync path; provisioning not configured so no inline provision.
    dispatch.schedule_organization_tenant_provision('org_new')
    assert tenant.status == 'pending'


def test_ensure_and_mark_tenant_ready_roundtrip() -> None:
    # Smoke-test helpers accept MagicMock sessions without raising on construction.
    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = None
    tenant = ensure_organization_tenant(db, auth_org_id='org_roundtrip')
    assert tenant.auth_org_id == 'org_roundtrip'
    db.add.assert_called()

    existing = MagicMock()
    existing.auth_org_id = 'org_roundtrip'
    db.query.return_value.filter.return_value.one_or_none.return_value = existing
    ready = mark_tenant_ready(
        db,
        auth_org_id='org_roundtrip',
        supabase_project_ref='ref1',
        database_url='postgresql+psycopg://t',
        supabase_url='https://ref1.supabase.co',
        supabase_service_role_key='key',
    )
    assert ready is existing
    assert existing.status == TENANT_STATUS_READY
