"""Backfill compliance_history for any gap_analyses missing a history row."""

from alembic import op

revision = '20260718_0020'
down_revision = '20260717_0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Every system-created gap (publication / business_change / policy_update) must
    # have a compliance_history row. Re-run safely for any gaps missed by phase_18
    # or by failed application-level upserts.
    op.execute(
        """
        insert into public.compliance_history (
            id,
            org_id,
            gap_analysis_id,
            policy_id,
            policy_name,
            regulatory_rules,
            trigger_type,
            incident_at,
            created_at
        )
        select
            md5(gap.id || '-compliance-history-backfill'),
            gap.org_id,
            gap.id,
            gap.policy_id,
            coalesce(nullif(trim(policy.name), ''), 'Untitled policy'),
            coalesce(
                (
                    select jsonb_agg(distinct trim(finding.rule))
                    from jsonb_array_elements(
                        case
                            when jsonb_typeof(
                                coalesce(gap.retrieval_metadata->'sentence_findings', '[]'::jsonb)
                            ) = 'array'
                            then coalesce(
                                gap.retrieval_metadata->'sentence_findings',
                                '[]'::jsonb
                            )
                            else '[]'::jsonb
                        end
                    ) as finding_row
                    cross join lateral (
                        select nullif(trim(coalesce(finding_row->>'rule', '')), '') as rule
                    ) as finding
                    where finding.rule is not null
                ),
                case
                    when nullif(trim(coalesce(gap.rule, '')), '') is not null
                    then jsonb_build_array(trim(gap.rule))
                    else '[]'::jsonb
                end
            ),
            nullif(trim(coalesce(gap.trigger_type, '')), ''),
            coalesce(gap.created_at, now()),
            now()
        from public.gap_analyses as gap
        left join public.policies as policy on policy.id = gap.policy_id
        left join public.compliance_history as history on history.gap_analysis_id = gap.id
        where history.id is null
        """
    )


def downgrade() -> None:
    # Keep history rows; they are the source of truth for the dashboard.
    pass
