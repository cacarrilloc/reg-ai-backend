-- Mirror of alembic phase_14_review_requested_at for tenant project bootstrap.
alter table public.policy_revisions
  add column if not exists review_requested_at timestamptz;

create index if not exists policy_revisions_review_requested_idx
  on public.policy_revisions (org_id, status, review_requested_at desc)
  where review_requested_at is not null;
