"""Track explicit user review requests on policy revisions."""

from alembic import op

revision = '20260705_0014'
down_revision = '20260703_0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.policy_revisions '
        'add column if not exists review_requested_at timestamptz'
    )
    op.execute(
        'create index if not exists policy_revisions_review_requested_idx '
        'on public.policy_revisions (org_id, status, review_requested_at desc) '
        'where review_requested_at is not null'
    )


def downgrade() -> None:
    op.execute('drop index if exists public.policy_revisions_review_requested_idx')
    op.execute(
        'alter table public.policy_revisions drop column if exists review_requested_at'
    )
