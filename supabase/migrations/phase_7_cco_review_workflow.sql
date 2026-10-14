-- Phase 7: CCO review workflow persistence for dashboards, decisions,
-- audit chains, SLA reminders, and escalations.
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
alter table public.policy_revisions add column if not exists sla_reminded_at timestamptz;
alter table public.policy_revisions add column if not exists escalated_at timestamptz;

create index if not exists policy_revisions_org_status_created_idx
  on public.policy_revisions (org_id, status, created_at desc);
create index if not exists policy_revisions_sla_reminder_idx
  on public.policy_revisions (status, created_at, sla_reminded_at);
create index if not exists policy_revisions_escalation_idx
  on public.policy_revisions (status, escalated_at, created_at);

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
