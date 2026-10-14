"""Add business change regulatory analysis document columns."""

from alembic import op

revision = '20260630_0011'
down_revision = '20260630_0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table public.business_changes add column if not exists analysis_status text "
        "not null default 'PENDING'"
    )
    op.execute(
        'alter table public.business_changes add column if not exists analysis_file_url text'
    )
    op.execute(
        'alter table public.business_changes add column if not exists analysis_generated_at timestamptz'
    )
    op.execute(
        'create index if not exists business_changes_org_analysis_status_idx '
        'on public.business_changes (org_id, analysis_status)'
    )


def downgrade() -> None:
    op.execute('drop index if exists public.business_changes_org_analysis_status_idx')
    op.execute(
        'alter table public.business_changes drop column if exists analysis_generated_at'
    )
    op.execute(
        'alter table public.business_changes drop column if exists analysis_file_url'
    )
    op.execute(
        'alter table public.business_changes drop column if exists analysis_status'
    )
