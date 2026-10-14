"""Additive Marketing Review Agent columns and tenant-schema indexes."""

from pathlib import Path

from alembic import op

revision = '20260817_0040'
down_revision = '20260813_0039'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_40_marketing_review_agent.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS public.marketing_reviews_publication_idx;
        DROP INDEX IF EXISTS public.marketing_reviews_org_status_idx;
        DROP INDEX IF EXISTS public.marketing_reviews_org_content_created_idx;
        DROP INDEX IF EXISTS public.marketing_contents_tags_gin;
        DROP INDEX IF EXISTS public.marketing_contents_org_format_idx;
        DROP INDEX IF EXISTS public.marketing_contents_org_status_idx;
        DROP INDEX IF EXISTS public.marketing_contents_org_created_idx;
        """
    )
