-- Per-organization Postgres schema isolation on the shared Supabase project.
-- Customer orgs get schema_name = {organization_name}_tenant; MASTER_ACCOUNT / internal orgs stay shared.

alter table public.organization_tenants
  add column if not exists schema_name text;

alter table public.organization_tenants
  add column if not exists isolation_mode text not null default 'shared';

alter table public.organization_tenants
  drop constraint if exists organization_tenants_isolation_mode_check;

alter table public.organization_tenants
  add constraint organization_tenants_isolation_mode_check
  check (isolation_mode in ('shared', 'schema', 'project'));

create unique index if not exists organization_tenants_schema_name_uidx
  on public.organization_tenants (schema_name)
  where schema_name is not null and schema_name <> 'public';
