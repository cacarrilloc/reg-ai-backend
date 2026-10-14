"""Persist business-change assessments and durable workflow event deliveries."""

from alembic import op

revision = '20260622_0005'
down_revision = '20260622_0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.business_changes add column if not exists change_date date')
    op.execute(
        "alter table public.business_changes add column if not exists assessment_rationale text not null default ''"
    )
    op.execute(
        'alter table public.business_changes add column if not exists assessment_model text')
    op.execute(
        "alter table public.business_changes add column if not exists assessment_mode text not "
        "null default 'RULE_FALLBACK'"
    )
    op.execute(
        'alter table public.business_changes add column if not exists assessed_at timestamptz')
    op.execute(
        'create index if not exists business_changes_org_flagged_idx '
        'on public.business_changes (org_id, flagged_at desc)'
    )
    op.execute(
        'create table if not exists public.business_change_event_deliveries ('
        'id text primary key, '
        'business_change_id text not null references public.business_changes(id) on delete cascade, '
        'org_id text not null, '
        'event_id text, '
        'sent_at timestamptz, '
        'created_at timestamptz not null default now(), '
        'unique (business_change_id)'
        ')'
    )
    op.execute(
        'create index if not exists business_change_event_deliveries_pending_idx '
        'on public.business_change_event_deliveries (created_at) where sent_at is null'
    )
    op.execute(
        'alter table public.business_change_event_deliveries enable row level security')
    op.execute(
        'drop policy if exists business_change_deliveries_internal_access '
        'on public.business_change_event_deliveries'
    )
    op.execute(
        'create policy business_change_deliveries_internal_access '
        'on public.business_change_event_deliveries for all '
        'using (public.is_internal_user()) with check (public.is_internal_user())'
    )


def downgrade() -> None:
    op.execute(
        'drop policy if exists business_change_deliveries_internal_access '
        'on public.business_change_event_deliveries'
    )
    op.execute('drop table if exists public.business_change_event_deliveries')
    op.execute('drop index if exists public.business_changes_org_flagged_idx')
    op.execute(
        'alter table public.business_changes drop column if exists assessed_at')
    op.execute(
        'alter table public.business_changes drop column if exists assessment_mode')
    op.execute(
        'alter table public.business_changes drop column if exists assessment_model')
    op.execute(
        'alter table public.business_changes drop column if exists assessment_rationale')
    op.execute(
        'alter table public.business_changes drop column if exists change_date')
