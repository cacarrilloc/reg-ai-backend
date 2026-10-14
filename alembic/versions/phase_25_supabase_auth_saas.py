"""Supabase Auth cutover (clerk_* -> auth_* renames) + SaaS control-plane tables."""

from pathlib import Path

from alembic import op

revision = '20260731_0025'
down_revision = '20260730_0024'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_25_supabase_auth_saas.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop table if exists public.usage_counters')
    op.execute('drop table if exists public.support_tickets')
    op.execute('drop table if exists public.marketing_reviews')
    op.execute('drop table if exists public.marketing_contents')
    op.execute('drop table if exists public.subscriptions')
    op.execute('drop table if exists public.billing_customers')
    op.execute('drop table if exists public.organization_entitlements')
    op.execute('drop table if exists public.organization_invites')

    op.execute('alter table public.organization_members drop column if exists status')
    op.execute('drop index if exists public.organizations_slug_key')
    op.execute('alter table public.organizations drop column if exists slug')

    op.execute(
        'alter table public.organization_tenants '
        'drop constraint if exists organization_tenants_auth_org_id_fkey'
    )
    op.execute(
        'alter table public.organization_members '
        'drop constraint if exists organization_members_auth_org_id_fkey'
    )

    op.execute(
        "do $$ begin "
        "if exists (select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'organization_tenants' and column_name = 'auth_org_id') then "
        'alter table public.organization_tenants rename column auth_org_id to clerk_org_id; '
        'end if; end $$;'
    )
    op.execute(
        "do $$ begin "
        "if exists (select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'organization_members' and column_name = 'auth_user_id') then "
        'alter table public.organization_members rename column auth_user_id to clerk_user_id; '
        'end if; end $$;'
    )
    op.execute(
        "do $$ begin "
        "if exists (select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'organization_members' and column_name = 'auth_org_id') then "
        'alter table public.organization_members rename column auth_org_id to clerk_org_id; '
        'end if; end $$;'
    )
    op.execute(
        "do $$ begin "
        "if exists (select 1 from information_schema.columns where table_schema = 'public' "
        "and table_name = 'organizations' and column_name = 'auth_org_id') then "
        'alter table public.organizations rename column auth_org_id to clerk_org_id; '
        'end if; end $$;'
    )
    op.execute('drop function if exists public.current_auth_org_id()')
