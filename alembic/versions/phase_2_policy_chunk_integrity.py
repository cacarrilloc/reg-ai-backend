"""Enforce unique chunk positions within an organization policy."""

from alembic import op

revision = '20260624_0006'
down_revision = '20260622_0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        'create unique index if not exists document_chunks_org_policy_chunk_key '
        'on public.document_chunks (org_id, policy_id, chunk_index) '
        'where policy_id is not null'
    )


def downgrade() -> None:
    op.execute('drop index if exists public.document_chunks_org_policy_chunk_key')
