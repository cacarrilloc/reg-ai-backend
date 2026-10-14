"""Per-org enterprise SSO connections and domain routing."""

from pathlib import Path

from alembic import op

revision = '20260807_0036'
down_revision = '20260805_0028'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_36_organization_sso.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop table if exists public.organization_sso_domains')
    op.execute('drop table if exists public.organization_sso_connections')
