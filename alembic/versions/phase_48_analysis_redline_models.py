"""Separate chat models for analysis and redlines."""

from pathlib import Path

from alembic import op

revision = '20261009_0048'
down_revision = '20260927_0046'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_48_analysis_redline_models.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        """
        alter table public.organization_ai_settings
            drop column if exists analysis_api_base,
            drop column if exists analysis_model,
            drop column if exists analysis_provider;
        """
    )
