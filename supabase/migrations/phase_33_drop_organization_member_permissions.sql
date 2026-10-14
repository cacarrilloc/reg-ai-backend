-- Per-member System permissions now live on organization_members.permissions_granted (JSON).
-- Drop the unused normalized override table from phase_31.

DROP TABLE IF EXISTS organization_member_permissions;
