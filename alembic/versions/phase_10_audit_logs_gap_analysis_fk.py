"""Drop audit_logs.gap_analysis_id FK incompatible with append-only audit rows."""

from alembic import op

revision = '20260630_0010'
down_revision = '20260629_0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.audit_logs drop constraint if exists audit_logs_gap_analysis_id_fkey'
    )


def downgrade() -> None:
    op.execute(
        "alter table public.audit_logs add constraint audit_logs_gap_analysis_id_fkey "
        "foreign key (gap_analysis_id) references public.gap_analyses(id) on delete set null"
    )
