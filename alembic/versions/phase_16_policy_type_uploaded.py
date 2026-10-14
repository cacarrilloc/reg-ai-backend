"""Rename internal policy type from WSP to uploaded."""

from alembic import op

revision = '20260714_0016'
down_revision = '20260713_0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "update public.policies "
        "set type = 'uploaded' "
        "where type = 'WSP'"
    )
    op.execute(
        "alter table public.policies "
        "alter column type set default 'uploaded'"
    )
    op.execute(
        "update public.document_chunks "
        "set document_type = 'uploaded' "
        "where document_type = 'WSP'"
    )


def downgrade() -> None:
    op.execute(
        "update public.policies "
        "set type = 'WSP' "
        "where type = 'uploaded'"
    )
    op.execute(
        "alter table public.policies "
        "alter column type set default 'WSP'"
    )
    op.execute(
        "update public.document_chunks "
        "set document_type = 'WSP' "
        "where document_type = 'uploaded'"
    )
