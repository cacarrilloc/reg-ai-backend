-- Wide per-org role permission matrix:
--   4 rows per organization (one per System role)
--   9 boolean columns (one per System permission)
--
-- Replaces the sparse (org, role, permission_key) row model.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'organization_role_permissions'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'organization_role_permissions'
          AND column_name = 'permission_key'
    ) THEN
        ALTER TABLE organization_role_permissions
            RENAME TO organization_role_permissions_legacy;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS organization_role_permissions (
    auth_org_id TEXT NOT NULL,
    role TEXT NOT NULL,
    manage_organization BOOLEAN NOT NULL DEFAULT FALSE,
    delete_organization BOOLEAN NOT NULL DEFAULT FALSE,
    read_billing BOOLEAN NOT NULL DEFAULT FALSE,
    manage_billing BOOLEAN NOT NULL DEFAULT FALSE,
    read_domains BOOLEAN NOT NULL DEFAULT FALSE,
    manage_domains BOOLEAN NOT NULL DEFAULT FALSE,
    manage_enterprise_connections BOOLEAN NOT NULL DEFAULT FALSE,
    read_members BOOLEAN NOT NULL DEFAULT FALSE,
    manage_members BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT (NOW() AT TIME ZONE 'utc'),
    PRIMARY KEY (auth_org_id, role)
);

CREATE INDEX IF NOT EXISTS idx_org_role_permissions_wide_org
    ON organization_role_permissions (auth_org_id);

-- Seed screenshot defaults for every known organization × role.
INSERT INTO organization_role_permissions (
    auth_org_id,
    role,
    manage_organization,
    delete_organization,
    read_billing,
    manage_billing,
    read_domains,
    manage_domains,
    manage_enterprise_connections,
    read_members,
    manage_members
)
SELECT
    o.auth_org_id,
    r.role,
    CASE WHEN r.role IN ('master_user', 'chief_compliance_officer') THEN TRUE ELSE FALSE END,
    CASE WHEN r.role = 'master_user' THEN TRUE ELSE FALSE END,
    CASE WHEN r.role IN ('master_user', 'product_manager') THEN TRUE ELSE FALSE END,
    CASE WHEN r.role = 'master_user' THEN TRUE ELSE FALSE END,
    CASE WHEN r.role = 'master_user' THEN TRUE ELSE FALSE END,
    CASE WHEN r.role = 'master_user' THEN TRUE ELSE FALSE END,
    CASE WHEN r.role = 'master_user' THEN TRUE ELSE FALSE END,
    TRUE,
    CASE WHEN r.role IN ('master_user', 'chief_compliance_officer') THEN TRUE ELSE FALSE END
FROM organizations AS o
CROSS JOIN (
    VALUES
        ('master_user'),
        ('product_manager'),
        ('chief_compliance_officer'),
        ('nominated_team_member')
) AS r(role)
ON CONFLICT (auth_org_id, role) DO NOTHING;

-- Overlay any previously customized grants from the legacy sparse table.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'organization_role_permissions_legacy'
    ) THEN
        WITH granted AS (
            SELECT
                auth_org_id,
                role,
                BOOL_OR(permission_key = 'org:sys_profile:manage' AND granted) AS manage_organization,
                BOOL_OR(permission_key = 'org:sys_profile:delete' AND granted) AS delete_organization,
                BOOL_OR(permission_key = 'org:sys_billing:read' AND granted) AS read_billing,
                BOOL_OR(permission_key = 'org:sys_billing:manage' AND granted) AS manage_billing,
                BOOL_OR(permission_key = 'org:sys_domains:read' AND granted) AS read_domains,
                BOOL_OR(permission_key = 'org:sys_domains:manage' AND granted) AS manage_domains,
                BOOL_OR(permission_key = 'org:sys_sso:manage' AND granted) AS manage_enterprise_connections,
                BOOL_OR(permission_key = 'org:sys_memberships:read' AND granted) AS read_members,
                BOOL_OR(permission_key = 'org:sys_memberships:manage' AND granted) AS manage_members
            FROM organization_role_permissions_legacy
            GROUP BY auth_org_id, role
        )
        UPDATE organization_role_permissions AS dest
        SET
            manage_organization = granted.manage_organization,
            delete_organization = granted.delete_organization,
            read_billing = granted.read_billing,
            manage_billing = granted.manage_billing,
            read_domains = granted.read_domains,
            manage_domains = granted.manage_domains,
            manage_enterprise_connections = granted.manage_enterprise_connections,
            read_members = granted.read_members,
            manage_members = granted.manage_members,
            updated_at = NOW() AT TIME ZONE 'utc'
        FROM granted
        WHERE dest.auth_org_id = granted.auth_org_id
          AND dest.role = granted.role;

        DROP TABLE organization_role_permissions_legacy;
    END IF;
END $$;
