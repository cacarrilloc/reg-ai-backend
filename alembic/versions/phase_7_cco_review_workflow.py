"""Phase 7 CCO review workflow persistence."""

from alembic import op

revision = '20260626_0008'
down_revision = '20260624_0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table public.policy_revisions add column if not exists org_id text")
    op.execute("alter table public.policy_revisions add column if not exists policy_id text")
    op.execute(
        "update public.policy_revisions revision "
        "set org_id = gap.org_id, policy_id = gap.policy_id "
        "from public.gap_analyses gap "
        "where revision.gap_analysis_id = gap.id "
        "and (revision.org_id is null or revision.policy_id is null)"
    )
    op.execute("alter table public.policy_revisions alter column org_id set not null")
    op.execute("alter table public.policy_revisions alter column policy_id set not null")
    op.execute("alter table public.policy_revisions add column if not exists sla_reminded_at timestamptz")
    op.execute("alter table public.policy_revisions add column if not exists escalated_at timestamptz")
    op.execute(
        "create index if not exists policy_revisions_org_status_created_idx "
        "on public.policy_revisions (org_id, status, created_at desc)"
    )
    op.execute(
        "create index if not exists policy_revisions_sla_reminder_idx "
        "on public.policy_revisions (status, created_at, sla_reminded_at)"
    )
    op.execute(
        "create index if not exists policy_revisions_escalation_idx "
        "on public.policy_revisions (status, escalated_at, created_at)"
    )
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'policy_revisions_policy_id_fkey'"
        ") then "
        "alter table public.policy_revisions add constraint policy_revisions_policy_id_fkey "
        "foreign key (policy_id) references public.policies(id) on delete cascade; "
        "end if; "
        "end $$"
    )
    op.execute(
        "create unique index if not exists gap_analyses_id_org_policy_key "
        "on public.gap_analyses (id, org_id, policy_id)"
    )
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'policy_revisions_gap_org_policy_fkey'"
        ") then "
        "alter table public.policy_revisions add constraint policy_revisions_gap_org_policy_fkey "
        "foreign key (gap_analysis_id, org_id, policy_id) "
        "references public.gap_analyses(id, org_id, policy_id) on delete cascade; "
        "end if; "
        "end $$"
    )

    op.execute("alter table public.audit_logs add column if not exists gap_analysis_id text")
    op.execute("alter table public.audit_logs add column if not exists notes text")
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'audit_logs_gap_analysis_id_fkey'"
        ") then "
        "alter table public.audit_logs add constraint audit_logs_gap_analysis_id_fkey "
        "foreign key (gap_analysis_id) references public.gap_analyses(id) on delete set null; "
        "end if; "
        "end $$"
    )
    op.execute(
        "create index if not exists audit_logs_org_revision_timestamp_idx "
        "on public.audit_logs (org_id, revision_id, timestamp desc)"
    )


def downgrade() -> None:
    op.execute('drop index if exists public.audit_logs_org_revision_timestamp_idx')
    op.execute('alter table public.audit_logs drop column if exists notes')
    op.execute('alter table public.audit_logs drop column if exists gap_analysis_id')
    op.execute('alter table public.policy_revisions drop constraint if exists policy_revisions_gap_org_policy_fkey')
    op.execute('drop index if exists public.gap_analyses_id_org_policy_key')
    op.execute('alter table public.policy_revisions drop constraint if exists policy_revisions_policy_id_fkey')
    op.execute('drop index if exists public.policy_revisions_escalation_idx')
    op.execute('drop index if exists public.policy_revisions_sla_reminder_idx')
    op.execute('drop index if exists public.policy_revisions_org_status_created_idx')
    op.execute('alter table public.policy_revisions drop column if exists escalated_at')
    op.execute('alter table public.policy_revisions drop column if exists sla_reminded_at')
    op.execute('alter table public.policy_revisions drop column if exists policy_id')
    op.execute('alter table public.policy_revisions drop column if exists org_id')
