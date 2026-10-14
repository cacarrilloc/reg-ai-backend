"""Add policy_name to document_chunks for regulator-scoped vector comparison."""

from alembic import op

revision = '20260703_0013'
down_revision = '20260703_0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.document_chunks add column if not exists policy_name text'
    )
    op.execute(
        'create index if not exists document_chunks_policy_name_idx '
        'on public.document_chunks (policy_name)'
    )
    op.execute(
        'create index if not exists document_chunks_org_policy_name_idx '
        'on public.document_chunks (org_id, policy_name)'
    )
    op.execute(
        """
        update public.document_chunks dc
        set policy_name = p.name
        from public.policies p
        where dc.policy_id = p.id
          and dc.policy_name is null
        """
    )
    op.execute(
        """
        update public.document_chunks dc
        set policy_name = upper(p.name)
        from public.policies p
        where dc.policy_id = p.id
          and dc.document_type = 'WSP'
          and upper(p.name) in ('FINRA', 'SEC', 'CFTC')
          and dc.policy_name is distinct from upper(p.name)
        """
    )
    op.execute(
        """
        update public.document_chunks dc
        set policy_name = (
          case
            when p.name ilike '%FINRA%' then 'FINRA'
            when p.name ilike '%CFTC%' then 'CFTC'
            when p.name ilike '%SEC%' then 'SEC'
            else dc.policy_name
          end
        )
        from public.policies p
        where dc.policy_id = p.id
          and dc.document_type = 'REGULATORY_REFERENCE'
        """
    )
    op.execute(
        """
        update public.document_chunks
        set policy_name = upper(metadata ->> 'source')
        where publication_id is not null
          and policy_name is null
          and metadata ->> 'source' is not null
        """
    )


def downgrade() -> None:
    op.execute('drop index if exists public.document_chunks_org_policy_name_idx')
    op.execute('drop index if exists public.document_chunks_policy_name_idx')
    op.execute(
        'alter table public.document_chunks drop column if exists policy_name'
    )
