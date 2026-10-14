-- Mirror of alembic phase_17_approved_reupload_reminders for tenant project bootstrap.
alter table public.policy_revisions
  add column if not exists reupload_reminded_at timestamptz;

alter table public.policy_revisions
  add column if not exists reuploaded_at timestamptz;

create index if not exists policy_revisions_reupload_reminder_idx
  on public.policy_revisions (status, reviewed_at, reupload_reminded_at)
  where status = 'APPROVED' and reuploaded_at is null;
