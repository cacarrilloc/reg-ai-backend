"""Add organization_members table for Clerk membership mirror."""

from pathlib import Path

from alembic import op

revision = '20260729_0022'
down_revision = '20260720_0021'
branch_labels = None
depends_on = None

_SQL = Path(__file__).resolve().parents[2] / 'supabase' / 'migrations' / 'phase_22_organization_members.sql'


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop policy if exists organization_members_org_access on public.organization_members')
    op.execute('drop index if exists organization_members_user_idx')
    op.execute('drop index if exists organization_members_org_idx')
    op.execute('drop table if exists public.organization_members')
