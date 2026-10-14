"""Phase 9 compliance automation: business change event summaries."""

from alembic import op

revision = '20260629_0009'
down_revision = '20260627_0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table public.business_changes add column if not exists event_summary text not null default ''"
    )


def downgrade() -> None:
    op.execute('alter table public.business_changes drop column if exists event_summary')
