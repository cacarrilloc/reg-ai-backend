"""Add trigger_type to compliance_history for dashboard Trigger badges."""

from alembic import op

revision = '20260717_0019'
down_revision = '20260717_0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        alter table public.compliance_history
        add column if not exists trigger_type text
        """
    )
    op.execute(
        """
        create index if not exists compliance_history_org_trigger_idx
        on public.compliance_history (org_id, trigger_type)
        """
    )
    # Backfill from linked gap analyses so existing history rows show the right badge.
    op.execute(
        """
        update public.compliance_history as history
        set trigger_type = gap.trigger_type
        from public.gap_analyses as gap
        where history.gap_analysis_id = gap.id
          and history.trigger_type is null
          and nullif(trim(gap.trigger_type), '') is not null
        """
    )


def downgrade() -> None:
    op.execute('drop index if exists public.compliance_history_org_trigger_idx')
    op.execute(
        'alter table public.compliance_history drop column if exists trigger_type'
    )
