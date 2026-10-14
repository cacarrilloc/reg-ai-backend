alter table public.business_changes add column if not exists change_date date;
alter table public.business_changes add column if not exists assessment_rationale text not null default '';
alter table public.business_changes add column if not exists assessment_model text;
alter table public.business_changes add column if not exists assessment_mode text not null default 'RULE_FALLBACK';
alter table public.business_changes add column if not exists assessed_at timestamptz;

create index if not exists business_changes_org_flagged_idx
  on public.business_changes (org_id, flagged_at desc);

create table if not exists public.business_change_event_deliveries (
  id text primary key default gen_random_uuid()::text,
  business_change_id text not null references public.business_changes(id) on delete cascade,
  org_id text not null,
  event_id text,
  sent_at timestamptz,
  created_at timestamptz not null default now(),
  unique (business_change_id)
);

create index if not exists business_change_event_deliveries_pending_idx
  on public.business_change_event_deliveries (created_at)
  where sent_at is null;

alter table public.business_change_event_deliveries enable row level security;

drop policy if exists business_change_deliveries_internal_access
  on public.business_change_event_deliveries;
create policy business_change_deliveries_internal_access
  on public.business_change_event_deliveries
  for all
  using (public.is_internal_user())
  with check (public.is_internal_user());
