-- phase_36: per-org enterprise SSO (SAML) connection + domain routing

create table if not exists public.organization_sso_connections (
  id text primary key,
  auth_org_id text not null unique,
  supabase_provider_id text,
  metadata_url text,
  metadata_xml text,
  sso_only boolean not null default false,
  jit_enabled boolean not null default true,
  default_role text not null default 'nominated_team_member',
  group_role_mappings jsonb not null default '{}'::jsonb,
  attribute_groups_key text not null default 'groups',
  status text not null default 'pending',
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint organization_sso_connections_status_check
    check (status in ('pending', 'active', 'error', 'disabled'))
);

create index if not exists idx_organization_sso_connections_auth_org
  on public.organization_sso_connections (auth_org_id);

create table if not exists public.organization_sso_domains (
  domain text primary key,
  auth_org_id text not null,
  sso_connection_id text not null
    references public.organization_sso_connections (id) on delete cascade,
  created_at timestamptz not null default now()
);

create index if not exists idx_organization_sso_domains_org
  on public.organization_sso_domains (auth_org_id);
