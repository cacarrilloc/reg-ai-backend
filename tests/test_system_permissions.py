"""System permissions catalog and screenshot System roles."""

from __future__ import annotations

from app.core.permissions import (
    ADMIN_SYSTEM_PERMISSIONS,
    DELETE_ORGANIZATION,
    MANAGE_MEMBERS,
    MANAGE_ORGANIZATION,
    MEMBER_SYSTEM_PERMISSIONS,
    READ_BILLING,
    READ_DOMAINS,
    READ_MEMBERS,
    ROLE_SYSTEM_PERMISSIONS,
    SYSTEM_PERMISSION_KEYS,
    SYSTEM_PERMISSIONS,
    permissions_for_role,
    permissions_for_roles,
    role_has_permission,
    roles_have_permission,
)
from app.core.roles import (
    ALL_ROLES,
    CHIEF_COMPLIANCE_OFFICER,
    MASTER_USER,
    NOMINATED_TEAM_MEMBER,
    PRODUCT_MANAGER,
    normalize_role,
)
from app.core.security import CurrentUser


def test_exactly_four_system_roles() -> None:
    assert ALL_ROLES == {
        MASTER_USER,
        PRODUCT_MANAGER,
        CHIEF_COMPLIANCE_OFFICER,
        NOMINATED_TEAM_MEMBER,
    }


def test_system_permissions_catalog_has_exactly_nine_entries() -> None:
    assert len(SYSTEM_PERMISSIONS) == 9
    assert len(SYSTEM_PERMISSION_KEYS) == 9
    assert [item.name for item in SYSTEM_PERMISSIONS] == [
        'Manage organization',
        'Delete organization',
        'Read billing',
        'Manage billing',
        'Read domains',
        'Manage domains',
        'Manage enterprise connections',
        'Read members',
        'Manage members',
    ]


def test_screenshot_permission_counts() -> None:
    assert len(permissions_for_role(MASTER_USER)) == 9
    assert len(permissions_for_role(PRODUCT_MANAGER)) == 2
    assert len(permissions_for_role(NOMINATED_TEAM_MEMBER)) == 1
    assert len(permissions_for_role(CHIEF_COMPLIANCE_OFFICER)) == 3


def test_master_has_all_system_permissions() -> None:
    assert permissions_for_role(MASTER_USER) == ADMIN_SYSTEM_PERMISSIONS == SYSTEM_PERMISSION_KEYS


def test_developer_alias_maps_to_master() -> None:
    assert normalize_role('developer_user') == MASTER_USER
    assert normalize_role('org:developer_user') == MASTER_USER
    assert len(permissions_for_role('developer_user')) == 9


def test_product_manager_two_read_permissions() -> None:
    assert permissions_for_role(PRODUCT_MANAGER) == frozenset({READ_BILLING, READ_MEMBERS})


def test_nominated_one_permission() -> None:
    assert permissions_for_role(NOMINATED_TEAM_MEMBER) == MEMBER_SYSTEM_PERMISSIONS == frozenset(
        {READ_MEMBERS}
    )


def test_cco_three_firm_ops_permissions() -> None:
    assert permissions_for_role(CHIEF_COMPLIANCE_OFFICER) == frozenset(
        {
            MANAGE_ORGANIZATION,
            READ_MEMBERS,
            MANAGE_MEMBERS,
        }
    )


def test_clerk_key_aliases_normalize() -> None:
    assert normalize_role('org:master_user') == MASTER_USER
    assert normalize_role('org:product_manager') == PRODUCT_MANAGER
    assert normalize_role('org:chief_compliance_officer') == CHIEF_COMPLIANCE_OFFICER
    assert normalize_role('org:nominated_team_member') == NOMINATED_TEAM_MEMBER
    assert normalize_role('org:admin') == CHIEF_COMPLIANCE_OFFICER
    assert normalize_role('org_viewer') == NOMINATED_TEAM_MEMBER
    assert normalize_role('admin_user') == MASTER_USER


def test_permissions_union_across_roles() -> None:
    granted = permissions_for_roles({PRODUCT_MANAGER, NOMINATED_TEAM_MEMBER})
    assert READ_BILLING in granted
    assert READ_MEMBERS in granted
    assert MANAGE_MEMBERS not in granted


def test_unknown_permission_never_grants() -> None:
    assert not role_has_permission(MASTER_USER, 'org:custom:invented')
    assert not roles_have_permission({MASTER_USER}, 'org:custom:invented')


def test_every_system_role_has_explicit_grant_entry() -> None:
    assert set(ROLE_SYSTEM_PERMISSIONS) == ALL_ROLES


def test_current_user_has_permission_helpers() -> None:
    member = CurrentUser(
        user_id='user-1',
        org_id='org-1',
        role=NOMINATED_TEAM_MEMBER,
        roles=frozenset({NOMINATED_TEAM_MEMBER}),
    )
    assert member.has_permission(READ_MEMBERS)
    assert not member.has_permission(READ_DOMAINS)
    assert not member.has_permission(MANAGE_MEMBERS)
    assert member.permissions() == frozenset({READ_MEMBERS})

    master = CurrentUser(
        user_id='user-2',
        org_id='org-1',
        role='org:master_user',
        roles=frozenset({'org:master_user'}),
    )
    assert master.has_permission(DELETE_ORGANIZATION)
    assert len(master.permissions()) == 9
