"""Track post-approval Policy Library re-upload reminders."""

from alembic import op

revision = '20260715_0017'
down_revision = '20260714_0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.policy_revisions '
        'add column if not exists reupload_reminded_at timestamptz'
    )
    op.execute(
        'alter table public.policy_revisions '
        'add column if not exists reuploaded_at timestamptz'
    )
    op.execute(
        'create index if not exists policy_revisions_reupload_reminder_idx '
        'on public.policy_revisions (status, reviewed_at, reupload_reminded_at) '
        "where status = 'APPROVED' and reuploaded_at is null"
    )


def downgrade() -> None:
    op.execute('drop index if exists public.policy_revisions_reupload_reminder_idx')
    op.execute(
        'alter table public.policy_revisions drop column if exists reuploaded_at'
    )
    op.execute(
        'alter table public.policy_revisions drop column if exists reupload_reminded_at'
    )
