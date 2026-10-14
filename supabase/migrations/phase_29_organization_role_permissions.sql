-- Per-organization System permission overrides (Member Permissions page).
-- When no rows exist for an org+role, screenshot defaults from app code apply.
-- Migrate removed developer_user assignments to master_user.

CREATE TABLE IF NOT EXISTS organization_role_permissions (
    id TEXT PRIMARY KEY,
    auth_org_id TEXT NOT NULL,
    role TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    granted BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT uq_org_role_permission UNIQUE (auth_org_id, role, permission_key)
);

CREATE INDEX IF NOT EXISTS idx_org_role_permissions_org
    ON organization_role_permissions (auth_org_id);

CREATE INDEX IF NOT EXISTS idx_org_role_permissions_org_role
    ON organization_role_permissions (auth_org_id, role);

UPDATE organization_members
SET role = 'master_user',
    updated_at = NOW() AT TIME ZONE 'utc'
WHERE role IN ('developer_user', 'org:developer_user');

UPDATE organization_invites
SET role = 'master_user'
WHERE role IN ('developer_user', 'org:developer_user')
  AND status = 'pending';
