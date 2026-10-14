from __future__ import annotations

from pathlib import Path

from alembic import op

revision = '20260609_0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    migration_sql = Path(
        __file__).parents[2] / 'supabase' / 'migrations' / 'phase_1_foundation.sql'
    op.execute(migration_sql.read_text())


def downgrade() -> None:
    op.execute('drop policy if exists org_storage_update on storage.objects')
    op.execute('drop policy if exists org_storage_insert on storage.objects')
    op.execute('drop policy if exists org_storage_select on storage.objects')
    op.execute('drop policy if exists audit_logs_org_access on public.audit_logs')
    op.execute(
        'drop policy if exists policy_revisions_org_access on public.policy_revisions')
    op.execute(
        'drop policy if exists gap_analyses_org_access on public.gap_analyses')
    op.execute(
        'drop policy if exists business_changes_org_access on public.business_changes')
    op.execute(
        'drop policy if exists document_chunks_org_access on public.document_chunks')
    op.execute(
        'drop policy if exists context_documents_org_access on public.context_documents')
    op.execute(
        'drop policy if exists publications_internal_write on public.regulatory_publications')
    op.execute(
        'drop policy if exists publications_read_authenticated on public.regulatory_publications')
    op.execute('drop policy if exists policies_org_access on public.policies')
    op.execute(
        'drop policy if exists organizations_org_access on public.organizations')
    op.execute('drop table if exists public.audit_logs')
    op.execute('drop table if exists public.policy_revisions')
    op.execute('drop table if exists public.gap_analyses')
    op.execute('drop table if exists public.business_changes')
    op.execute('drop table if exists public.document_chunks')
    op.execute('drop table if exists public.context_documents')
    op.execute('drop table if exists public.regulatory_publications')
    op.execute('drop table if exists public.policies')
    op.execute('drop table if exists public.organizations')
    op.execute('drop function if exists public.is_internal_user()')
    op.execute('drop function if exists public.current_internal_role()')
    op.execute('drop function if exists public.current_clerk_org_id()')
