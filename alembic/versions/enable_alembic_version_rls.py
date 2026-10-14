"""Protect Alembic's migration-version table with RLS."""

from alembic import op

# Disable naming lint for Alembic revision variables which must use these names
# pylint: disable=invalid-name
revision = '20260622_0003'
down_revision = '20260620_0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Enable row level security on Alembic's version table."""
    op.execute('alter table public.alembic_version enable row level security')


def downgrade() -> None:
    """Disable row level security on Alembic's version table."""
    op.execute('alter table public.alembic_version disable row level security')
