-- Store per-member System permissions as JSON on organization_members.
-- Shape: JSON array of granted permission keys, e.g.
--   ["org:sys_memberships:read", "org:sys_billing:read"]
-- Role defaults remain in organization_role_permissions (System Role matrix).

ALTER TABLE organization_members
    ADD COLUMN IF NOT EXISTS permissions_granted JSONB;

-- Move any existing per-member override rows onto the member JSON column.
UPDATE organization_members AS m
SET permissions_granted = agg.permissions_granted,
    updated_at = NOW() AT TIME ZONE 'utc'
FROM (
    SELECT
        auth_org_id,
        auth_user_id,
        jsonb_agg(permission_key ORDER BY permission_key) AS permissions_granted
    FROM organization_member_permissions
    WHERE granted IS TRUE
    GROUP BY auth_org_id, auth_user_id
) AS agg
WHERE m.auth_org_id = agg.auth_org_id
  AND m.auth_user_id = agg.auth_user_id
  AND (m.permissions_granted IS NULL OR m.permissions_granted = 'null'::jsonb);
