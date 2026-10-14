"""Add supabase_url and service role key to organization_tenants."""

from pathlib import Path

from alembic import op

revision = '20260730_0024'
down_revision = '20260729_0023'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_24_organization_tenant_credentials.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        'alter table public.organization_tenants '
        'drop column if exists supabase_service_role_key'
    )
    op.execute(
        'alter table public.organization_tenants '
        'drop column if exists supabase_url'
    )
