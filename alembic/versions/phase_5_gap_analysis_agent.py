"""Persist Phase 5 gap-analysis trigger, retrieval, and model provenance."""

from alembic import op

revision = '20260624_0007'
down_revision = '20260624_0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "alter table public.gap_analyses add column if not exists trigger_type text")
    op.execute(
        "alter table public.gap_analyses add column if not exists trigger_id text")
    op.execute(
        "alter table public.gap_analyses add column if not exists retrieval_metadata jsonb not null default '{}'::jsonb"
    )
    op.execute(
        'alter table public.gap_analyses add column if not exists analysis_model text')
    op.execute(
        "alter table public.gap_analyses add column if not exists analysis_mode text not null default 'RULE_FALLBACK'"
    )
    op.execute(
        "update public.gap_analyses set trigger_type = case "
        "when publication_id is not null then 'publication' else 'business_change' end "
        "where trigger_type is null"
    )
    op.execute(
        'update public.gap_analyses set trigger_id = coalesce(publication_id, business_change_id) '
        "where trigger_id is null or trigger_id = ''"
    )
    op.execute(
        'alter table public.gap_analyses alter column trigger_type set not null')
    op.execute(
        'alter table public.gap_analyses alter column trigger_id set not null')
    op.execute(
        'create unique index if not exists gap_analyses_org_trigger_policy_key '
        'on public.gap_analyses (org_id, trigger_type, trigger_id, policy_id)'
    )
    op.execute(
        'create index if not exists gap_analyses_org_impact_created_idx '
        'on public.gap_analyses (org_id, impact_score, created_at desc)'
    )
    op.execute(
        'create index if not exists gap_analyses_org_trigger_created_idx '
        'on public.gap_analyses (org_id, trigger_type, created_at desc)'
    )


def downgrade() -> None:
    op.execute('drop index if exists public.gap_analyses_org_trigger_created_idx')
    op.execute('drop index if exists public.gap_analyses_org_impact_created_idx')
    op.execute('drop index if exists public.gap_analyses_org_trigger_policy_key')
    op.execute(
        'alter table public.gap_analyses drop column if exists analysis_mode')
    op.execute(
        'alter table public.gap_analyses drop column if exists analysis_model')
    op.execute(
        'alter table public.gap_analyses drop column if exists retrieval_metadata')
    op.execute('alter table public.gap_analyses drop column if exists trigger_id')
    op.execute(
        'alter table public.gap_analyses drop column if exists trigger_type')
