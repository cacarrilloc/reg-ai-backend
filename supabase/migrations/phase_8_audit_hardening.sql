alter table public.policy_revisions add column if not exists org_id text;
alter table public.policy_revisions add column if not exists policy_id text;

update public.policy_revisions revision
set
  org_id = gap.org_id,
  policy_id = gap.policy_id
from public.gap_analyses gap
where revision.gap_analysis_id = gap.id
  and (revision.org_id is null or revision.policy_id is null);

alter table public.policy_revisions alter column org_id set not null;
alter table public.policy_revisions alter column policy_id set not null;

create index if not exists policy_revisions_org_status_created_idx
  on public.policy_revisions (org_id, status, created_at desc);

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'policy_revisions_policy_id_fkey'
  ) then
    alter table public.policy_revisions
      add constraint policy_revisions_policy_id_fkey
      foreign key (policy_id) references public.policies(id) on delete cascade;
  end if;
end $$;

create unique index if not exists gap_analyses_id_org_policy_key
  on public.gap_analyses (id, org_id, policy_id);

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'policy_revisions_gap_org_policy_fkey'
  ) then
    alter table public.policy_revisions
      add constraint policy_revisions_gap_org_policy_fkey
      foreign key (gap_analysis_id, org_id, policy_id)
      references public.gap_analyses(id, org_id, policy_id) on delete cascade;
  end if;
end $$;

alter table public.audit_logs add column if not exists gap_analysis_id text;
alter table public.audit_logs add column if not exists notes text;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'audit_logs_gap_analysis_id_fkey'
  ) then
    alter table public.audit_logs
      add constraint audit_logs_gap_analysis_id_fkey
      foreign key (gap_analysis_id) references public.gap_analyses(id) on delete set null;
  end if;
end $$;

create index if not exists audit_logs_org_revision_timestamp_idx
  on public.audit_logs (org_id, revision_id, timestamp desc);

create or replace function public.prevent_audit_log_mutation()
returns trigger
language plpgsql
as $$
begin
  raise exception 'audit_logs are append-only';
end;
$$;

drop trigger if exists audit_logs_no_update on public.audit_logs;
create trigger audit_logs_no_update
  before update on public.audit_logs
  for each row execute function public.prevent_audit_log_mutation();

drop trigger if exists audit_logs_no_delete on public.audit_logs;
create trigger audit_logs_no_delete
  before delete on public.audit_logs
  for each row execute function public.prevent_audit_log_mutation();

alter table public.organizations enable row level security;
alter table public.organizations force row level security;
alter table public.policies enable row level security;
alter table public.policies force row level security;
alter table public.regulatory_publications enable row level security;
alter table public.regulatory_publications force row level security;
alter table public.context_documents enable row level security;
alter table public.context_documents force row level security;
alter table public.document_chunks enable row level security;
alter table public.document_chunks force row level security;
alter table public.business_changes enable row level security;
alter table public.business_changes force row level security;
alter table public.business_change_event_deliveries enable row level security;
alter table public.business_change_event_deliveries force row level security;
alter table public.publication_event_deliveries enable row level security;
alter table public.publication_event_deliveries force row level security;
alter table public.gap_analyses enable row level security;
alter table public.gap_analyses force row level security;
alter table public.policy_revisions enable row level security;
alter table public.policy_revisions force row level security;
alter table public.audit_logs enable row level security;
alter table public.audit_logs force row level security;

drop policy if exists publications_read_authenticated on public.regulatory_publications;
drop policy if exists regulatory_publications_org_select on public.regulatory_publications;
create policy regulatory_publications_org_select on public.regulatory_publications
  for select
  using (
    public.is_internal_user()
    or exists (
      select 1
      from public.organizations organization
      where organization.clerk_org_id = public.current_clerk_org_id()
        and exists (
          select 1
          from jsonb_array_elements_text(
            coalesce(organization.regulatory_scope -> 'sources', '[]'::jsonb)
          ) source(value)
          where upper(source.value) = upper(regulatory_publications.source)
        )
    )
  );

drop policy if exists publication_deliveries_internal_access on public.publication_event_deliveries;
drop policy if exists publication_deliveries_org_access on public.publication_event_deliveries;
create policy publication_deliveries_org_access on public.publication_event_deliveries
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists business_change_deliveries_internal_access on public.business_change_event_deliveries;
drop policy if exists business_change_deliveries_org_access on public.business_change_event_deliveries;
create policy business_change_deliveries_org_access on public.business_change_event_deliveries
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists policy_revisions_org_access on public.policy_revisions;
create policy policy_revisions_org_access on public.policy_revisions
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());
