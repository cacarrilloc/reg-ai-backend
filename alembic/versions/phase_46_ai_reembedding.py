"""Embedding-model switch: re-embed jobs (public) and staged vectors (every tenant schema)."""

from pathlib import Path

from alembic import op

revision = '20260927_0046'
down_revision = '20260924_0045'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_46_ai_reembedding.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        """
        do $$
        declare
            tenant_schema text;
        begin
            for tenant_schema in
                select schema_name
                from information_schema.schemata
                where schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
            loop
                execute format('drop table if exists %I.ai_reembed_vectors', tenant_schema);
            end loop;
        end
        $$;
        """
    )
    op.execute('drop table if exists public.ai_reembed_vectors')
    op.execute('drop function if exists public.apply_phase_46_ai_reembed_vectors(text)')
    op.execute('drop table if exists public.organization_ai_reembed_jobs')
