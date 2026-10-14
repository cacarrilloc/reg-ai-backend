"""Phase 8 audit trail and RLS hardening."""

from alembic import op

revision = '20260627_0008'
down_revision = '20260626_0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("alter table public.policy_revisions add column if not exists org_id text")
    op.execute("alter table public.policy_revisions add column if not exists policy_id text")
    op.execute(
        "update public.policy_revisions revision "
        "set org_id = gap.org_id, policy_id = gap.policy_id "
        "from public.gap_analyses gap "
        "where revision.gap_analysis_id = gap.id "
        "and (revision.org_id is null or revision.policy_id is null)"
    )
    op.execute("alter table public.policy_revisions alter column org_id set not null")
    op.execute("alter table public.policy_revisions alter column policy_id set not null")
    op.execute(
        "create index if not exists policy_revisions_org_status_created_idx "
        "on public.policy_revisions (org_id, status, created_at desc)"
    )
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'policy_revisions_policy_id_fkey'"
        ") then "
        "alter table public.policy_revisions add constraint policy_revisions_policy_id_fkey "
        "foreign key (policy_id) references public.policies(id) on delete cascade; "
        "end if; "
        "end $$"
    )
    op.execute(
        "create unique index if not exists gap_analyses_id_org_policy_key "
        "on public.gap_analyses (id, org_id, policy_id)"
    )
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'policy_revisions_gap_org_policy_fkey'"
        ") then "
        "alter table public.policy_revisions add constraint policy_revisions_gap_org_policy_fkey "
        "foreign key (gap_analysis_id, org_id, policy_id) "
        "references public.gap_analyses(id, org_id, policy_id) on delete cascade; "
        "end if; "
        "end $$"
    )

    op.execute("alter table public.audit_logs add column if not exists gap_analysis_id text")
    op.execute("alter table public.audit_logs add column if not exists notes text")
    op.execute(
        "do $$ "
        "begin "
        "if not exists ("
        "select 1 from pg_constraint where conname = 'audit_logs_gap_analysis_id_fkey'"
        ") then "
        "alter table public.audit_logs add constraint audit_logs_gap_analysis_id_fkey "
        "foreign key (gap_analysis_id) references public.gap_analyses(id) on delete set null; "
        "end if; "
        "end $$"
    )
    op.execute(
        "create index if not exists audit_logs_org_revision_timestamp_idx "
        "on public.audit_logs (org_id, revision_id, timestamp desc)"
    )
    op.execute(
        "create or replace function public.prevent_audit_log_mutation() "
        "returns trigger "
        "language plpgsql "
        "as $$ "
        "begin "
        "raise exception 'audit_logs are append-only'; "
        "end; "
        "$$"
    )
    op.execute("drop trigger if exists audit_logs_no_update on public.audit_logs")
    op.execute(
        "create trigger audit_logs_no_update "
        "before update on public.audit_logs "
        "for each row execute function public.prevent_audit_log_mutation()"
    )
    op.execute("drop trigger if exists audit_logs_no_delete on public.audit_logs")
    op.execute(
        "create trigger audit_logs_no_delete "
        "before delete on public.audit_logs "
        "for each row execute function public.prevent_audit_log_mutation()"
    )

    rls_tables = [
        'organizations',
        'policies',
        'regulatory_publications',
        'context_documents',
        'document_chunks',
        'business_changes',
        'business_change_event_deliveries',
        'publication_event_deliveries',
        'gap_analyses',
        'policy_revisions',
        'audit_logs',
    ]
    for table in rls_tables:
        op.execute(f'alter table public.{table} enable row level security')
        op.execute(f'alter table public.{table} force row level security')

    op.execute("drop policy if exists publications_read_authenticated on public.regulatory_publications")
    op.execute("drop policy if exists regulatory_publications_org_select on public.regulatory_publications")
    op.execute(
        "create policy regulatory_publications_org_select on public.regulatory_publications "
        "for select using ("
        "public.is_internal_user() "
        "or exists ("
        "select 1 from public.organizations organization "
        "where organization.clerk_org_id = public.current_clerk_org_id() "
        "and exists ("
        "select 1 "
        "from jsonb_array_elements_text("
        "coalesce(organization.regulatory_scope -> 'sources', '[]'::jsonb)"
        ") source(value) "
        "where upper(source.value) = upper(regulatory_publications.source)"
        ")"
        ")"
        ")"
    )
    op.execute("drop policy if exists publication_deliveries_internal_access on public.publication_event_deliveries")
    op.execute("drop policy if exists publication_deliveries_org_access on public.publication_event_deliveries")
    op.execute(
        "create policy publication_deliveries_org_access on public.publication_event_deliveries "
        "for all using (public.is_internal_user() or org_id = public.current_clerk_org_id()) "
        "with check (public.is_internal_user() or org_id = public.current_clerk_org_id())"
    )
    op.execute(
        "drop policy if exists business_change_deliveries_internal_access "
        "on public.business_change_event_deliveries"
    )
    op.execute(
        "drop policy if exists business_change_deliveries_org_access "
        "on public.business_change_event_deliveries"
    )
    op.execute(
        "create policy business_change_deliveries_org_access on public.business_change_event_deliveries "
        "for all using (public.is_internal_user() or org_id = public.current_clerk_org_id()) "
        "with check (public.is_internal_user() or org_id = public.current_clerk_org_id())"
    )
    op.execute("drop policy if exists policy_revisions_org_access on public.policy_revisions")
    op.execute(
        "create policy policy_revisions_org_access on public.policy_revisions "
        "for all using (public.is_internal_user() or org_id = public.current_clerk_org_id()) "
        "with check (public.is_internal_user() or org_id = public.current_clerk_org_id())"
    )


def downgrade() -> None:
    op.execute('drop trigger if exists audit_logs_no_delete on public.audit_logs')
    op.execute('drop trigger if exists audit_logs_no_update on public.audit_logs')
    op.execute('drop function if exists public.prevent_audit_log_mutation()')
    op.execute('drop policy if exists regulatory_publications_org_select on public.regulatory_publications')
    op.execute('drop policy if exists publication_deliveries_org_access on public.publication_event_deliveries')
    op.execute('drop policy if exists business_change_deliveries_org_access on public.business_change_event_deliveries')
