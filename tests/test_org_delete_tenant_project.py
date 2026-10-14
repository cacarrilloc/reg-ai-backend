"""Organization delete / tenant project teardown guards."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.services.org_purge_service import resolve_dedicated_tenant_project_ref


def test_resolve_dedicated_tenant_skips_platform_ref(monkeypatch) -> None:
    tenant = MagicMock()
    tenant.supabase_project_ref = 'ugnfslbacesddjiyjhvp'

    monkeypatch.setattr(
        'app.services.org_purge_service.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.org_purge_service.platform_supabase_project_ref',
        lambda settings=None: 'ugnfslbacesddjiyjhvp',
    )

    assert resolve_dedicated_tenant_project_ref(
        MagicMock(), auth_org_id='org_customer_1'
    ) is None


def test_resolve_dedicated_tenant_returns_customer_ref(monkeypatch) -> None:
    tenant = MagicMock()
    tenant.supabase_project_ref = 'customerprojectref1'

    monkeypatch.setattr(
        'app.services.org_purge_service.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.org_purge_service.platform_supabase_project_ref',
        lambda settings=None: 'ugnfslbacesddjiyjhvp',
    )

    assert (
        resolve_dedicated_tenant_project_ref(MagicMock(), auth_org_id='org_customer_1')
        == 'customerprojectref1'
    )


def test_resolve_dedicated_tenant_skips_internal_auth_org_id(monkeypatch) -> None:
    tenant = MagicMock()
    tenant.supabase_project_ref = 'customerprojectref1'

    monkeypatch.setattr(
        'app.services.org_purge_service.get_organization_tenant',
        lambda *args, **kwargs: tenant,
    )
    monkeypatch.setattr(
        'app.services.org_purge_service.platform_supabase_project_ref',
        lambda settings=None: 'ugnfslbacesddjiyjhvp',
    )

    assert (
        resolve_dedicated_tenant_project_ref(
            MagicMock(), auth_org_id='org_internal_development'
        )
        is None
    )
