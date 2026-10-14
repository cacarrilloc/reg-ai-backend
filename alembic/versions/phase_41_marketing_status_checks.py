"""Allow marketing review statuses beyond the original stub CHECKs."""

from pathlib import Path

from alembic import op

revision = '20260817_0041'
down_revision = '20260817_0040'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_41_marketing_status_checks.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE public.marketing_contents DROP CONSTRAINT IF EXISTS marketing_contents_status_check;
        ALTER TABLE public.marketing_reviews DROP CONSTRAINT IF EXISTS marketing_reviews_status_check;
        ALTER TABLE public.marketing_contents
            ADD CONSTRAINT marketing_contents_status_check
            CHECK (status IN ('draft', 'in_review', 'approved', 'rejected'));
        ALTER TABLE public.marketing_reviews
            ADD CONSTRAINT marketing_reviews_status_check
            CHECK (status IN ('pending', 'approved', 'rejected'));
        """
    )
