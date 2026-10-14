alter table public.business_changes
  add column if not exists analysis_status text not null default 'PENDING';

alter table public.business_changes
  add column if not exists analysis_file_url text;

alter table public.business_changes
  add column if not exists analysis_generated_at timestamptz;

create index if not exists business_changes_org_analysis_status_idx
  on public.business_changes (org_id, analysis_status);
