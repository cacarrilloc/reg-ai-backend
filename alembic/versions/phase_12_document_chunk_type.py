"""Add document_type to document_chunks for WSP vs REGULATORY_REFERENCE labeling."""

from alembic import op

revision = '20260703_0012'
down_revision = '20260630_0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'alter table public.document_chunks add column if not exists document_type text'
    )
    op.execute(
        'create index if not exists document_chunks_document_type_idx '
        'on public.document_chunks (document_type)'
    )
    op.execute(
        'create index if not exists document_chunks_org_document_type_idx '
        'on public.document_chunks (org_id, document_type)'
    )
    op.execute(
        """
        update public.document_chunks dc
        set document_type = upper(p.type)
        from public.policies p
        where dc.policy_id = p.id
          and dc.document_type is null
        """
    )
    op.execute(
        """
        update public.document_chunks
        set document_type = 'REGULATORY_REFERENCE'
        where document_type is null
          and (
            metadata ->> 'reference_type' = 'REGULATORY_REFERENCE'
            or metadata ? 'source'
          )
          and policy_id is not null
        """
    )
    op.execute(
        """
        update public.document_chunks
        set document_type = 'REGULATORY_PUBLICATION'
        where publication_id is not null
          and document_type is null
        """
    )
    op.execute(
        """
        update public.document_chunks
        set document_type = 'CONTEXT_DOCUMENT'
        where context_document_id is not null
          and document_type is null
        """
    )
    op.execute(
        """
        update public.document_chunks
        set document_type = 'WSP'
        where policy_id is not null
          and document_type is null
        """
    )


def downgrade() -> None:
    op.execute('drop index if exists public.document_chunks_org_document_type_idx')
    op.execute('drop index if exists public.document_chunks_document_type_idx')
    op.execute(
        'alter table public.document_chunks drop column if exists document_type'
    )
