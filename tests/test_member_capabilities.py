import pytest
from fastapi import HTTPException

from app.core.member_capabilities import (
    DELETE_RESOURCES,
    MANAGE_NEWSLETTER_SUBSCRIBERS,
    capabilities_for_role,
    exclude_roles_from_functionality,
    member_can,
    register_functionality,
    require_delete_resources,
)
from app.core.roles import (
    CHIEF_COMPLIANCE_OFFICER,
    MASTER_USER,
    NOMINATED_TEAM_MEMBER,
    PRODUCT_MANAGER,
)
from app.core.security import CurrentUser


def test_delete_resources_allows_master_and_cco_only() -> None:
    assert member_can(MASTER_USER, DELETE_RESOURCES) is True
    assert member_can(CHIEF_COMPLIANCE_OFFICER, DELETE_RESOURCES) is True
    assert member_can('cco', DELETE_RESOURCES) is True
    assert member_can('org:master_user', DELETE_RESOURCES) is True
    assert member_can(PRODUCT_MANAGER, DELETE_RESOURCES) is False
    assert member_can(NOMINATED_TEAM_MEMBER, DELETE_RESOURCES) is False
    assert member_can('reviewer', DELETE_RESOURCES) is False


def test_excluded_member_type_wins_over_allowlist() -> None:
    key = 'future_feature'
    register_functionality(
        key,
        allowed_roles=(MASTER_USER, PRODUCT_MANAGER),
        excluded_roles=(PRODUCT_MANAGER,),
    )
    try:
        assert member_can(MASTER_USER, key) is True
        assert member_can(PRODUCT_MANAGER, key) is False
        exclude_roles_from_functionality(key, MASTER_USER)
        assert member_can(MASTER_USER, key) is False
    finally:
        from app.core import member_capabilities as caps

        caps._ALLOWED_ROLES.pop(key, None)
        caps._EXCLUDED_ROLES.pop(key, None)


def test_capabilities_for_role_lists_granted_keys() -> None:
    master_caps = capabilities_for_role(MASTER_USER)
    pm_caps = capabilities_for_role(PRODUCT_MANAGER)
    assert DELETE_RESOURCES in master_caps
    assert MANAGE_NEWSLETTER_SUBSCRIBERS in master_caps
    assert DELETE_RESOURCES not in pm_caps
    assert MANAGE_NEWSLETTER_SUBSCRIBERS not in pm_caps


def test_require_delete_resources_rejects_nominated() -> None:
    user = CurrentUser(
        user_id='u1',
        org_id='org_acme',
        role=NOMINATED_TEAM_MEMBER,
        roles=frozenset({NOMINATED_TEAM_MEMBER}),
    )
    with pytest.raises(HTTPException) as exc:
        require_delete_resources(user)
    assert exc.value.status_code == 403


def test_require_delete_resources_allows_cco() -> None:
    user = CurrentUser(
        user_id='u1',
        org_id='org_acme',
        role=CHIEF_COMPLIANCE_OFFICER,
        roles=frozenset({CHIEF_COMPLIANCE_OFFICER}),
    )
    assert require_delete_resources(user) is user
