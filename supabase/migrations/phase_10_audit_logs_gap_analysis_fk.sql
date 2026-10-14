-- audit_logs are append-only (no UPDATE/DELETE). The phase 7/8 FK attempted to null out
-- gap_analysis_id when parent gap_analyses rows are deleted, which requires updating
-- audit_logs and conflicts with prevent_audit_log_mutation(). Keep gap_analysis_id as a
-- historical reference without FK enforcement.
alter table public.audit_logs
  drop constraint if exists audit_logs_gap_analysis_id_fkey;
