-- Phase 5: identify each trigger, preserve retrieval/model provenance, and make
-- one gap result per policy document idempotent for every tenant-scoped trigger.
alter table public.gap_analyses add column if not exists trigger_type text;
alter table public.gap_analyses add column if not exists trigger_id text;
alter table public.gap_analyses add column if not exists retrieval_metadata jsonb not null default '{}'::jsonb;
alter table public.gap_analyses add column if not exists analysis_model text;
alter table public.gap_analyses add column if not exists analysis_mode text not null default 'RULE_FALLBACK';

update public.gap_analyses
set trigger_type = case when publication_id is not null then 'publication' else 'business_change' end
where trigger_type is null;

update public.gap_analyses
set trigger_id = coalesce(publication_id, business_change_id)
where trigger_id is null or trigger_id = '';

alter table public.gap_analyses alter column trigger_type set not null;
alter table public.gap_analyses alter column trigger_id set not null;

create unique index if not exists gap_analyses_org_trigger_policy_key
  on public.gap_analyses (org_id, trigger_type, trigger_id, policy_id);
create index if not exists gap_analyses_org_impact_created_idx
  on public.gap_analyses (org_id, impact_score, created_at desc);
create index if not exists gap_analyses_org_trigger_created_idx
  on public.gap_analyses (org_id, trigger_type, created_at desc);
