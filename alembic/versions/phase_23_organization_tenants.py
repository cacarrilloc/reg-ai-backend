"""Add organization_tenants control-plane registry for per-org app stacks."""

from pathlib import Path

from alembic import op

revision = '20260729_0023'
down_revision = '20260729_0022'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_23_organization_tenants.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop policy if exists organization_tenants_org_access on public.organization_tenants')
    op.execute('drop index if exists organization_tenants_status_idx')
    op.execute('drop table if exists public.organization_tenants')
