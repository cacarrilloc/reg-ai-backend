"""Add schema_name and isolation_mode to organization_tenants."""

from pathlib import Path

from alembic import op

revision = '20260813_0039'
down_revision = '20260807_0037'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_39_organization_tenant_schema.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop index if exists public.organization_tenants_schema_name_uidx')
    op.execute(
        'alter table public.organization_tenants '
        'drop constraint if exists organization_tenants_isolation_mode_check'
    )
    op.execute('alter table public.organization_tenants drop column if exists isolation_mode')
    op.execute('alter table public.organization_tenants drop column if exists schema_name')
