-- Mirror of alembic phase_18_compliance_history for tenant project bootstrap.
create table if not exists public.compliance_history (
  id text primary key,
  org_id text not null,
  gap_analysis_id text references public.gap_analyses(id) on delete set null,
  policy_id text references public.policies(id) on delete set null,
  policy_name text not null,
  regulatory_rules jsonb not null default '[]'::jsonb,
  incident_at timestamptz not null,
  created_at timestamptz not null default now()
);

create unique index if not exists uq_compliance_history_gap_analysis_id
  on public.compliance_history (gap_analysis_id)
  where gap_analysis_id is not null;

create index if not exists compliance_history_org_incident_idx
  on public.compliance_history (org_id, incident_at desc);

alter table public.compliance_history enable row level security;
alter table public.compliance_history force row level security;

drop policy if exists compliance_history_org_access on public.compliance_history;
create policy compliance_history_org_access on public.compliance_history
  for all using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());
