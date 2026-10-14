"""Add organizations.logo_url for org branding."""

from pathlib import Path

from alembic import op

revision = '20260807_0037'
down_revision = '20260807_0036'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_37_organization_logo.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('alter table public.organizations drop column if exists logo_url')
