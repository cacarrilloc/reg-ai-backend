-- Per-member System permission overrides (Member permission editor).
-- When rows exist for an org+user, that full set replaces role defaults at runtime.

CREATE TABLE IF NOT EXISTS organization_member_permissions (
    id TEXT PRIMARY KEY,
    auth_org_id TEXT NOT NULL,
    auth_user_id TEXT NOT NULL,
    permission_key TEXT NOT NULL,
    granted BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    CONSTRAINT uq_org_member_permission UNIQUE (auth_org_id, auth_user_id, permission_key)
);

CREATE INDEX IF NOT EXISTS idx_org_member_permissions_org
    ON organization_member_permissions (auth_org_id);

CREATE INDEX IF NOT EXISTS idx_org_member_permissions_org_user
    ON organization_member_permissions (auth_org_id, auth_user_id);
