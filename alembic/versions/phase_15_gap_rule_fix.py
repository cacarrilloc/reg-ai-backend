"""Add rule and fix columns to gap_analyses for structured LLM findings."""

from alembic import op

revision = '20260713_0015'
down_revision = '20260705_0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.gap_analyses '
        'add column if not exists rule text'
    )
    op.execute(
        'alter table public.gap_analyses '
        'add column if not exists fix text'
    )


def downgrade() -> None:
    op.execute('alter table public.gap_analyses drop column if exists fix')
    op.execute('alter table public.gap_analyses drop column if exists rule')
