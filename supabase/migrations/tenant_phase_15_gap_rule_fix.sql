-- Mirror of alembic phase_15_gap_rule_fix for tenant project bootstrap.
alter table public.gap_analyses
  add column if not exists rule text;

alter table public.gap_analyses
  add column if not exists fix text;
