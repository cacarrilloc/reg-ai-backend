"""BYOK: org AI settings (public) and encrypted provider keys (every tenant schema)."""

from pathlib import Path

from alembic import op

revision = '20260924_0045'
down_revision = '20260908_0044'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_45_organization_ai_settings.sql'
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
                execute format('drop table if exists %I.ai_provider_credentials', tenant_schema);
            end loop;
        end
        $$;
        """
    )
    op.execute('drop table if exists public.ai_provider_credentials')
    op.execute('drop function if exists public.apply_phase_45_ai_provider_credentials(text)')
    op.execute('drop table if exists public.organization_ai_settings')
