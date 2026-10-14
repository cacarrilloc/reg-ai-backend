"""Allow hyphens in user_profiles.username."""

from pathlib import Path

from alembic import op

revision = '20260805_0028'
down_revision = '20260805_0027'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_28_username_hyphens.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        'alter table public.user_profiles '
        'drop constraint if exists user_profiles_username_format'
    )
    op.execute(
        "alter table public.user_profiles "
        "add constraint user_profiles_username_format check ("
        "username ~ '^[a-z0-9_]{3,32}$')"
    )
