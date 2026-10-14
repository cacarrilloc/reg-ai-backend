-- Count of TRUE permission columns on each role row (always maintained by Postgres).

ALTER TABLE organization_role_permissions
    ADD COLUMN IF NOT EXISTS total_enabled_permissions INTEGER
    GENERATED ALWAYS AS (
        (CASE WHEN manage_organization THEN 1 ELSE 0 END)
        + (CASE WHEN delete_organization THEN 1 ELSE 0 END)
        + (CASE WHEN read_billing THEN 1 ELSE 0 END)
        + (CASE WHEN manage_billing THEN 1 ELSE 0 END)
        + (CASE WHEN read_domains THEN 1 ELSE 0 END)
        + (CASE WHEN manage_domains THEN 1 ELSE 0 END)
        + (CASE WHEN manage_enterprise_connections THEN 1 ELSE 0 END)
        + (CASE WHEN read_members THEN 1 ELSE 0 END)
        + (CASE WHEN manage_members THEN 1 ELSE 0 END)
    ) STORED;
