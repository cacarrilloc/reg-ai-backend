-- Mirror of alembic phase_19_compliance_history_trigger_type for tenant bootstrap.
alter table public.compliance_history
  add column if not exists trigger_type text;

create index if not exists compliance_history_org_trigger_idx
  on public.compliance_history (org_id, trigger_type);
