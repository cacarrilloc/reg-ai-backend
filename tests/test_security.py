from app.core.config import Settings
from app.core.roles import CHIEF_COMPLIANCE_OFFICER, MASTER_USER, PRODUCT_MANAGER
from app.core.security import (
    current_user_from_claims,
    should_use_auth_bypass,
    should_use_local_header_override,
)


def test_supabase_issuer_values_allow_trailing_slash_mismatch() -> None:
    settings = Settings(SUPABASE_JWT_ISSUER='https://example.supabase.co/auth/v1/')

    assert settings.clerk_issuer_values == [
        'https://example.supabase.co/auth/v1',
        'https://example.supabase.co/auth/v1/',
    ]


def test_auth_bypass_allows_local_requests_when_clerk_is_configured_without_bearer() -> None:
    settings = Settings(
        ENVIRONMENT='local',
        AUTH_BYPASS_ENABLED=True,
        CLERK_ISSUER='https://example.clerk.accounts.dev/',
        CLERK_JWKS_URL='https://example.clerk.accounts.dev/.well-known/jwks.json',
    )

    assert should_use_auth_bypass(settings, authorization=None) is True
    assert should_use_auth_bypass(
        settings, authorization='Bearer real-token') is False


def test_auth_bypass_is_disabled_in_production() -> None:
    settings = Settings(
        ENVIRONMENT='production',
        AUTH_BYPASS_ENABLED=True,
        CLERK_ISSUER='https://example.clerk.accounts.dev/',
        CLERK_JWKS_URL='https://example.clerk.accounts.dev/.well-known/jwks.json',
    )

    assert should_use_auth_bypass(settings, authorization=None) is False


def test_local_header_override_allows_seeded_org_even_with_clerk_token() -> None:
    settings = Settings(
        ENVIRONMENT='local',
        AUTH_BYPASS_ENABLED=True,
        CLERK_ISSUER='https://example.clerk.accounts.dev/',
        CLERK_JWKS_URL='https://example.clerk.accounts.dev/.well-known/jwks.json',
    )

    assert should_use_local_header_override(settings, 'org_seeded') is True


def test_local_header_override_is_disabled_without_org_or_in_production() -> None:
    assert should_use_local_header_override(
        Settings(ENVIRONMENT='local', AUTH_BYPASS_ENABLED=True),
        None,
    ) is False
    assert should_use_local_header_override(
        Settings(ENVIRONMENT='production', AUTH_BYPASS_ENABLED=True),
        'org_seeded',
    ) is False


def test_organization_admin_role_maps_to_chief_compliance_officer() -> None:
    settings = Settings()

    user = current_user_from_claims(
        {
            'sub': 'user_admin',
            'org_id': 'org_example',
            'org_role': 'org:admin',
        },
        settings,
    )

    assert user.role == CHIEF_COMPLIANCE_OFFICER


def test_explicit_app_metadata_role_overrides_organization_role_claim() -> None:
    settings = Settings()

    user = current_user_from_claims(
        {
            'sub': 'user_admin',
            'org_id': 'org_example',
            'org_role': 'org:admin',
            'app_metadata': {'role': PRODUCT_MANAGER},
        },
        settings,
    )

    assert user.role == PRODUCT_MANAGER
    assert user.has_any_role({CHIEF_COMPLIANCE_OFFICER}) is True


def test_clerk_session_token_v2_organization_admin_is_authorized() -> None:
    settings = Settings(CLERK_ROLE_CLAIM='metadata.role')

    user = current_user_from_claims(
        {
            'sub': 'user_admin',
            'o': {'id': 'org_example', 'rol': 'admin'},
            'metadata': {'role': PRODUCT_MANAGER},
        },
        settings,
    )

    assert user.org_id == 'org_example'
    assert user.has_any_role({MASTER_USER}) is True


def test_clerk_custom_cco_role_is_authorized_for_the_active_organization() -> None:
    settings = Settings(CLERK_ROLE_CLAIM='metadata.role')

    user = current_user_from_claims(
        {
            'sub': 'user_cco',
            'o': {'id': 'org_example', 'rol': 'org:chief_compliance_officer'},
        },
        settings,
    )

    assert user.org_id == 'org_example'
    assert user.role == CHIEF_COMPLIANCE_OFFICER
    assert user.has_any_role({CHIEF_COMPLIANCE_OFFICER}) is True


def test_supabase_authenticated_system_role_is_ignored() -> None:
    settings = Settings()

    user = current_user_from_claims(
        {
            'sub': 'user_cco',
            'role': 'authenticated',
            'app_metadata': {
                'active_org_id': 'org_example',
                'role': CHIEF_COMPLIANCE_OFFICER,
            },
        },
        settings,
    )

    assert user.org_id == 'org_example'
    assert user.role == CHIEF_COMPLIANCE_OFFICER
    assert 'authenticated' not in user.roles
    assert user.has_any_role({CHIEF_COMPLIANCE_OFFICER}) is True


def test_supabase_authenticated_only_leaves_empty_app_roles() -> None:
    settings = Settings()

    user = current_user_from_claims(
        {
            'sub': 'user_pending',
            'role': 'authenticated',
            'app_metadata': {'active_org_id': 'org_example'},
        },
        settings,
    )

    assert user.org_id == 'org_example'
    assert user.roles == frozenset()
    assert user.has_any_role({CHIEF_COMPLIANCE_OFFICER}) is False


def test_enrich_membership_expands_roles_only_for_internal_orgs(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core.permissions import SYSTEM_PERMISSION_KEYS
    from app.core.roles import BILLING_ROLES, MASTER_USER, NOMINATED_TEAM_MEMBER
    from app.core.security import CurrentUser, clear_membership_role_cache, enrich_user_from_membership

    clear_membership_role_cache()

    class _Query:
        def __init__(self, row):
            self._row = row

        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return self._row

        def all(self):
            return [self._row] if self._row is not None else []

    class _Db:
        def __init__(self, row):
            self._row = row
            self.queries = 0

        def query(self, model):
            self.queries += 1
            return _Query(self._row)

        def close(self):
            return None

        def flush(self, _objects=None):
            return None

        def add(self, _row):
            return None

    member = SimpleNamespace(role=NOMINATED_TEAM_MEMBER, permissions_granted=None)
    db = _Db(member)
    monkeypatch.setattr('app.db.session.SessionLocal', lambda: db)

    internal_user = CurrentUser(
        user_id='user_1',
        org_id='org_internal_development',
        role=NOMINATED_TEAM_MEMBER,
        roles=frozenset({NOMINATED_TEAM_MEMBER}),
    )
    enriched_internal = enrich_user_from_membership(internal_user)
    assert enriched_internal.has_any_role(BILLING_ROLES)
    assert enriched_internal.roles == frozenset({MASTER_USER})
    assert enriched_internal.permissions() == SYSTEM_PERMISSION_KEYS
    # Internal orgs must not open a platform DB session on every request.
    assert db.queries == 0

    customer_user = CurrentUser(
        user_id='user_1',
        org_id='org_customer_abc',
        role=NOMINATED_TEAM_MEMBER,
        roles=frozenset({NOMINATED_TEAM_MEMBER}),
    )
    enriched_customer = enrich_user_from_membership(customer_user)
    assert enriched_customer.has_any_role(BILLING_ROLES) is False
    assert enriched_customer.roles == frozenset({NOMINATED_TEAM_MEMBER})
    # Membership row + optional role-permission override lookup.
    assert db.queries >= 1

    first_queries = db.queries
    # Second call for the same customer membership should hit the in-memory cache.
    enrich_user_from_membership(customer_user)
    assert db.queries == first_queries
