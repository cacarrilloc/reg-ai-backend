"""Add compliance_history table for gap incident metadata."""

from alembic import op

revision = '20260717_0018'
down_revision = '20260715_0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        create table if not exists public.compliance_history (
            id text primary key,
            org_id text not null,
            gap_analysis_id text references public.gap_analyses(id) on delete set null,
            policy_id text references public.policies(id) on delete set null,
            policy_name text not null,
            regulatory_rules jsonb not null default '[]'::jsonb,
            incident_at timestamptz not null,
            created_at timestamptz not null default now()
        )
        """
    )
    op.execute(
        """
        create unique index if not exists uq_compliance_history_gap_analysis_id
        on public.compliance_history (gap_analysis_id)
        where gap_analysis_id is not null
        """
    )
    op.execute(
        """
        create index if not exists compliance_history_org_incident_idx
        on public.compliance_history (org_id, incident_at desc)
        """
    )
    op.execute('alter table public.compliance_history enable row level security')
    op.execute('alter table public.compliance_history force row level security')
    op.execute('drop policy if exists compliance_history_org_access on public.compliance_history')
    op.execute(
        """
        create policy compliance_history_org_access on public.compliance_history
        for all using (public.is_internal_user() or org_id = public.current_clerk_org_id())
        with check (public.is_internal_user() or org_id = public.current_clerk_org_id())
        """
    )
    # Backfill from existing gap analyses (one history row per gap incident).
    op.execute(
        """
        insert into public.compliance_history (
            id,
            org_id,
            gap_analysis_id,
            policy_id,
            policy_name,
            regulatory_rules,
            incident_at,
            created_at
        )
        select
            md5(gap.id || '-compliance-history'),
            gap.org_id,
            gap.id,
            gap.policy_id,
            coalesce(nullif(trim(policy.name), ''), 'Untitled policy'),
            case
                when jsonb_typeof(coalesce(gap.regulatory_citations, '[]'::jsonb)) = 'array'
                     and jsonb_array_length(coalesce(gap.regulatory_citations, '[]'::jsonb)) > 0
                then (
                    select coalesce(
                        jsonb_agg(
                            distinct trim(
                                case
                                    when nullif(trim(coalesce(citation->>'source', '')), '') is not null
                                         and nullif(trim(coalesce(citation->>'reference', '')), '') is not null
                                         and position(
                                             lower(trim(citation->>'source'))
                                             in lower(trim(citation->>'reference'))
                                         ) = 0
                                    then trim(citation->>'source') || ': ' || trim(citation->>'reference')
                                    when nullif(trim(coalesce(citation->>'reference', '')), '') is not null
                                    then trim(citation->>'reference')
                                    else trim(coalesce(citation->>'source', ''))
                                end
                            )
                        ) filter (
                            where trim(
                                case
                                    when nullif(trim(coalesce(citation->>'source', '')), '') is not null
                                         and nullif(trim(coalesce(citation->>'reference', '')), '') is not null
                                         and position(
                                             lower(trim(citation->>'source'))
                                             in lower(trim(citation->>'reference'))
                                         ) = 0
                                    then trim(citation->>'source') || ': ' || trim(citation->>'reference')
                                    when nullif(trim(coalesce(citation->>'reference', '')), '') is not null
                                    then trim(citation->>'reference')
                                    else trim(coalesce(citation->>'source', ''))
                                end
                            ) <> ''
                        ),
                        '[]'::jsonb
                    )
                    from jsonb_array_elements(coalesce(gap.regulatory_citations, '[]'::jsonb)) as citation
                )
                when nullif(trim(coalesce(gap.rule, '')), '') is not null
                then jsonb_build_array(trim(gap.rule))
                else '[]'::jsonb
            end,
            gap.created_at,
            now()
        from public.gap_analyses gap
        left join public.policies policy on policy.id = gap.policy_id
        where not exists (
            select 1
            from public.compliance_history history
            where history.gap_analysis_id = gap.id
        )
        """
    )


def downgrade() -> None:
    op.execute('drop policy if exists compliance_history_org_access on public.compliance_history')
    op.execute('drop index if exists public.compliance_history_org_incident_idx')
    op.execute('drop index if exists public.uq_compliance_history_gap_analysis_id')
    op.execute('drop table if exists public.compliance_history')
