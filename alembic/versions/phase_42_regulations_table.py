"""Additive regulations table split from policies."""

from pathlib import Path

from alembic import op

revision = '20260821_0042'
down_revision = '20260817_0041'
branch_labels = None
depends_on = None

_SQL = (
    Path(__file__).resolve().parents[2]
    / 'supabase'
    / 'migrations'
    / 'phase_42_regulations_table.sql'
)


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding='utf-8'))


def downgrade() -> None:
    op.execute(
        """
        alter table public.policies drop constraint if exists policies_internal_type_check;
        alter table public.document_chunks drop constraint if exists document_chunks_regulation_id_fkey;
        update public.document_chunks
        set policy_id = regulation_id
        where regulation_id is not null and policy_id is null;
        insert into public.policies (
            id, org_id, name, type, version, status, file_url, created_at, updated_at
        )
        select
            id, org_id, name, type, version, status, file_url, created_at, updated_at
        from public.regulations
        where rule_code is null
        on conflict (id) do nothing;
        alter table public.document_chunks drop column if exists regulation_id;
        drop table if exists public.regulations;
        drop function if exists public.apply_phase_42_regulations(text);
        """
    )
