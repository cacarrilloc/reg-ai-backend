-- Clerk Organization profiles: local membership mirror for tenant access control.
-- organizations.clerk_org_id + name stay synced from Clerk; members are sub-users.

create table if not exists public.organization_members (
  id text primary key default gen_random_uuid()::text,
  clerk_org_id text not null references public.organizations(clerk_org_id) on delete cascade,
  clerk_user_id text not null,
  role text not null default 'org:member',
  email text,
  full_name text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint uq_org_members_org_user unique (clerk_org_id, clerk_user_id)
);

create index if not exists organization_members_org_idx
  on public.organization_members (clerk_org_id);

create index if not exists organization_members_user_idx
  on public.organization_members (clerk_user_id);

alter table public.organization_members enable row level security;
alter table public.organization_members force row level security;

drop policy if exists organization_members_org_access on public.organization_members;
create policy organization_members_org_access
  on public.organization_members
  for all
  using (
    public.is_internal_user() or clerk_org_id = public.current_clerk_org_id()
  )
  with check (
    public.is_internal_user() or clerk_org_id = public.current_clerk_org_id()
  );
