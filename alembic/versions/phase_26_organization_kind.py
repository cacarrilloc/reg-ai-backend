"""Add organizations.kind (internal | external) for env/tenant binding."""

from pathlib import Path

from alembic import op

revision = '20260805_0026'
down_revision = '20260731_0025'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_26_organization_kind.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute('drop index if exists public.organizations_kind_idx')
    op.execute(
        'alter table public.organizations '
        'drop constraint if exists organizations_kind_check'
    )
    op.execute('alter table public.organizations drop column if exists kind')
