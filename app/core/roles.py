from __future__ import annotations

# ---------------------------------------------------------------------------
# System roles (Roles & Permissions)
#
# Keys use Clerk-style org: prefixes in the dashboard; canonical app values are
# snake_case without the prefix after normalize_role().
#
#   master_user               — Full platform / firm access (9 System permissions)
#   product_manager           — Product + client view (2)
#   chief_compliance_officer  — Customer CCO; reviews; approval chain (3)
#   nominated_team_member     — Scoped reviewer (1)
#
# developer_user was removed (Dev/Staging/Production orgs replace that split).
# ---------------------------------------------------------------------------

MASTER_USER = 'master_user'
PRODUCT_MANAGER = 'product_manager'
CHIEF_COMPLIANCE_OFFICER = 'chief_compliance_officer'
NOMINATED_TEAM_MEMBER = 'nominated_team_member'

# Legacy constant — maps to master_user via normalize_role().
DEVELOPER_USER = 'developer_user'

# Clerk-style keys as shown in the Roles UI.
CLERK_MASTER_USER = 'org:master_user'
CLERK_DEVELOPER_USER = 'org:developer_user'
CLERK_PRODUCT_MANAGER = 'org:product_manager'
CLERK_CHIEF_COMPLIANCE_OFFICER = 'org:chief_compliance_officer'
CLERK_NOMINATED_TEAM_MEMBER = 'org:nominated_team_member'

CLERK_CUSTOMER_ROLE_KEYS = frozenset({
    CLERK_CHIEF_COMPLIANCE_OFFICER,
    CLERK_NOMINATED_TEAM_MEMBER,
})

# Supabase Auth / Postgres roles — never treat these as Reg AI IAM roles.
SUPABASE_SYSTEM_ROLES = frozenset({
    'authenticated',
    'anon',
    'service_role',
    'supabase_admin',
})

# Roles that may be assigned on any organization (including external customers).
ALL_ROLES = {
    MASTER_USER,
    PRODUCT_MANAGER,
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
}

# Historically "internal" product roles (also inviteable on external orgs now).
INTERNAL_ROLES = {
    MASTER_USER,
    PRODUCT_MANAGER,
}

CUSTOMER_ROLES = {
    MASTER_USER,
    PRODUCT_MANAGER,
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
}

# Who may create client organizations (internal only).
ADMIN_ORG_CREATE_ROLES = {
    MASTER_USER,
}

# Firm / platform org management (settings, invites, delete).
ORG_MANAGEMENT_ROLES = {
    CHIEF_COMPLIANCE_OFFICER,
    MASTER_USER,
}

# Org newsletter subscriber list/delete in Organization Settings.
# Prefer app.core.member_capabilities for new functionality gates.
NEWSLETTER_SUBSCRIBER_MANAGER_ROLES = ORG_MANAGEMENT_ROLES


def can_manage_org_newsletter_subscribers(role: str | None) -> bool:
    """True for Master and CCO membership roles (not internal-env Master expansion)."""
    return normalize_role(role) in NEWSLETTER_SUBSCRIBER_MANAGER_ROLES


BILLING_ROLES = ORG_MANAGEMENT_ROLES

# Approval-chain configuration (CCO + Master).
APPROVAL_CHAIN_MANAGER_ROLES = {
    CHIEF_COMPLIANCE_OFFICER,
    MASTER_USER,
}

# View + action the Review Queue / redlines.
REVIEW_ACTION_ROLES = {
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
    *INTERNAL_ROLES,
}
REVIEW_ACCESS_ROLES = REVIEW_ACTION_ROLES

BUSINESS_CHANGE_CREATOR_ROLES = {
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
    MASTER_USER,
}

PRODUCT_1 = 'product_1'
PRODUCT_2 = 'product_2'

# Reserved Internal Development/Staging/Production: every member is treated as
# Master User for access checks (full System permissions).
INTERNAL_ENV_FULL_ACCESS_ROLES = frozenset({MASTER_USER})

# Default membership when a customer creates / is provisioned an org.
DEFAULT_CUSTOMER_CREATOR_ROLE = MASTER_USER
DEFAULT_INVITE_ROLE = NOMINATED_TEAM_MEMBER


def is_supabase_system_role(role: str | None) -> bool:
    if not role:
        return False
    return role.strip().lower() in SUPABASE_SYSTEM_ROLES


def is_app_role(role: str | None) -> bool:
    """Return True when ``role`` is a Reg AI IAM role (not a Supabase system role)."""
    if not role or is_supabase_system_role(role):
        return False
    return normalize_role(role) in ALL_ROLES


def normalize_role(role: str | None) -> str:
    """Normalize a role string to one of the System roles."""
    if not role:
        return PRODUCT_MANAGER

    normalized = role.strip().lower().replace('-', '_').replace(' ', '_')
    if normalized in SUPABASE_SYSTEM_ROLES:
        return normalized

    # Strip optional org: Clerk prefix for matching.
    bare = normalized[4:] if normalized.startswith('org:') else normalized

    aliases = {
        'master': MASTER_USER,
        'master_user': MASTER_USER,
        CLERK_MASTER_USER: MASTER_USER,
        # Removed developer role → Master User (Dev/Staging/Production replace it).
        'developer': MASTER_USER,
        'developer_user': MASTER_USER,
        CLERK_DEVELOPER_USER: MASTER_USER,
        'product_manager': PRODUCT_MANAGER,
        'pm': PRODUCT_MANAGER,
        CLERK_PRODUCT_MANAGER: PRODUCT_MANAGER,
        'cco': CHIEF_COMPLIANCE_OFFICER,
        'chief_compliance_officer': CHIEF_COMPLIANCE_OFFICER,
        CLERK_CHIEF_COMPLIANCE_OFFICER: CHIEF_COMPLIANCE_OFFICER,
        'nominated_team_member': NOMINATED_TEAM_MEMBER,
        CLERK_NOMINATED_TEAM_MEMBER: NOMINATED_TEAM_MEMBER,
        'team_member': NOMINATED_TEAM_MEMBER,
        'reviewer': NOMINATED_TEAM_MEMBER,
        'editor': NOMINATED_TEAM_MEMBER,
        'admin': MASTER_USER,
        'admin_user': MASTER_USER,
        'org:admin': CHIEF_COMPLIANCE_OFFICER,
        'org_admin': CHIEF_COMPLIANCE_OFFICER,
        'owner': CHIEF_COMPLIANCE_OFFICER,
        'viewer': NOMINATED_TEAM_MEMBER,
        'org_viewer': NOMINATED_TEAM_MEMBER,
        'org:member': NOMINATED_TEAM_MEMBER,
        'org_member': NOMINATED_TEAM_MEMBER,
        'org_editor': NOMINATED_TEAM_MEMBER,
    }
    if normalized in aliases:
        return aliases[normalized]
    if bare in aliases:
        return aliases[bare]
    return aliases.get(normalized, bare if bare in ALL_ROLES else normalized)
