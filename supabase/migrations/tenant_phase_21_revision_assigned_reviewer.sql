-- Persist per-revision "Assign to" contact fields on policy_revisions.
-- reviewer_id already stores the assignee user id; name/email were only mirrored
-- onto organizations.approval_chain (org-wide sticky), which lost older assignees.

alter table public.policy_revisions
  add column if not exists assigned_reviewer_name text;

alter table public.policy_revisions
  add column if not exists assigned_reviewer_email text;
