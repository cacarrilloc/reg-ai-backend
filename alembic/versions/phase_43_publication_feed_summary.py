"""Persist LLM summaries for the Regulatory Feed table."""

from pathlib import Path

from alembic import op

revision = '20260904_0043'
down_revision = '20260821_0042'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_43_publication_feed_summary.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('alter table public.regulatory_publications drop column if exists feed_summary')
