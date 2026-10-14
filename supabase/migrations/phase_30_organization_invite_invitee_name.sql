-- Optional display name supplied when inviting a teammate.

ALTER TABLE organization_invites
  ADD COLUMN IF NOT EXISTS invitee_name TEXT;
