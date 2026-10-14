-- Control-plane registry: bind each Clerk Organization to its app stack
-- (dedicated Supabase DB + frontend/backend URLs).

create table if not exists public.organization_tenants (
  id text primary key default gen_random_uuid()::text,
  clerk_org_id text not null unique
    references public.organizations(clerk_org_id) on delete cascade,
  status text not null default 'pending'
    check (status in ('pending', 'provisioning', 'ready', 'failed')),
  supabase_project_ref text,
  database_url text,
  backend_url text,
  frontend_url text,
  provision_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists organization_tenants_status_idx
  on public.organization_tenants (status);

alter table public.organization_tenants enable row level security;
alter table public.organization_tenants force row level security;

drop policy if exists organization_tenants_org_access on public.organization_tenants;
create policy organization_tenants_org_access
  on public.organization_tenants
  for all
  using (
    public.is_internal_user() or clerk_org_id = public.current_clerk_org_id()
  )
  with check (
    public.is_internal_user() or clerk_org_id = public.current_clerk_org_id()
  );
