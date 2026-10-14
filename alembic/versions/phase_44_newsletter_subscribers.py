"""Per-organization newsletter subscriber lists."""

from pathlib import Path

from alembic import op

revision = '20260908_0044'
down_revision = '20260904_0043'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_44_newsletter_subscribers.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop table if exists public.general_newsletter_subscribers')
    op.execute('drop table if exists public.newsletter_subscribers')
    op.execute('drop function if exists public.apply_phase_44_newsletter_subscribers(text)')
