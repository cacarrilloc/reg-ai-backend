-- Drop unused publication_event_deliveries outbox table.
-- Gap analysis for regulatory publications is owned by the regulatory
-- compliance workflow cron; this outbox was never populated in practice.

drop policy if exists publication_deliveries_org_access
  on public.publication_event_deliveries;
drop policy if exists publication_deliveries_internal_access
  on public.publication_event_deliveries;
drop index if exists publication_event_deliveries_pending_idx;
drop table if exists public.publication_event_deliveries;
