"""Add user_profiles for unique username <-> email accounts."""

from pathlib import Path

from alembic import op

revision = '20260805_0027'
down_revision = '20260805_0026'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_27_user_profiles.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop policy if exists user_profiles_self_access on public.user_profiles')
    op.execute('drop table if exists public.user_profiles')
