-- Phase 9: business change event summaries for CCO dashboard
alter table public.business_changes
  add column if not exists event_summary text not null default '';
