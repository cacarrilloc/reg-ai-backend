"""Add Phase 2 ingestion indexes and enforce policy version uniqueness."""

from alembic import op

revision = '20260620_0002'
down_revision = '20260609_0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'create unique index if not exists policies_org_name_version_key '
        'on public.policies (org_id, name, version)'
    )
    op.execute(
        'create index if not exists document_chunks_org_policy_chunk_idx '
        'on public.document_chunks (org_id, policy_id, chunk_index) '
        'where policy_id is not null'
    )
    op.execute(
        'create index if not exists context_documents_org_type_created_idx '
        'on public.context_documents (org_id, document_type, created_at desc)'
    )


def downgrade() -> None:
    op.execute(
        'drop index if exists public.context_documents_org_type_created_idx')
    op.execute('drop index if exists public.document_chunks_org_policy_chunk_idx')
    op.execute('drop index if exists public.policies_org_name_version_key')
