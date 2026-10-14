-- Phase 25: Supabase Auth cutover + SaaS control-plane tables (billing,
-- invites, entitlements, marketing review, support, usage metering).

-- 1. Rename clerk_* identifier columns to auth_* on the tables that still use
--    the old naming. Guarded so the migration is safe to re-run and safe to
--    run against a database that was bootstrapped after the rename already
--    landed in models.

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'organizations' and column_name = 'clerk_org_id'
  ) then
    alter table public.organizations rename column clerk_org_id to auth_org_id;
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'organization_members' and column_name = 'clerk_org_id'
  ) then
    alter table public.organization_members rename column clerk_org_id to auth_org_id;
  end if;
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'organization_members' and column_name = 'clerk_user_id'
  ) then
    alter table public.organization_members rename column clerk_user_id to auth_user_id;
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'organization_tenants' and column_name = 'clerk_org_id'
  ) then
    alter table public.organization_tenants rename column clerk_org_id to auth_org_id;
  end if;
end $$;

-- 2. current_auth_org_id() replaces current_clerk_org_id(). The old name is
--    kept as a thin wrapper so any policy/trigger that has not been updated
--    yet keeps working.
create or replace function public.current_auth_org_id()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(auth.jwt() ->> 'org_id', ''),
    nullif(auth.jwt() ->> 'orgId', ''),
    nullif(auth.jwt() #>> '{app_metadata,active_org_id}', ''),
    nullif(auth.jwt() #>> '{app_metadata,org_id}', ''),
    nullif(auth.jwt() #>> '{o,id}', '')
  );
$$;

create or replace function public.current_clerk_org_id()
returns text
language sql
stable
as $$
  select public.current_auth_org_id();
$$;

-- 3. organizations: add slug (nullable, unique) and refresh dependent index.
alter table public.organizations add column if not exists slug text;

do $$
begin
  if not exists (
    select 1 from pg_indexes
    where schemaname = 'public' and indexname = 'organizations_slug_key'
  ) then
    create unique index organizations_slug_key on public.organizations (slug) where slug is not null;
  end if;
end $$;

drop index if exists public.organizations_clerk_org_id_idx;
create index if not exists organizations_auth_org_id_idx on public.organizations(auth_org_id);

-- 4. organization_members: add status column.
alter table public.organization_members
  add column if not exists status text not null default 'active';

-- 5. Re-point foreign keys / policies that referenced organizations(clerk_org_id)
--    or organization.clerk_org_id directly (column has been renamed above).

do $$
begin
  if exists (
    select 1 from pg_constraint where conname = 'organization_members_clerk_org_id_fkey'
  ) then
    alter table public.organization_members
      drop constraint organization_members_clerk_org_id_fkey;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'organization_members_auth_org_id_fkey'
  ) then
    alter table public.organization_members
      add constraint organization_members_auth_org_id_fkey
      foreign key (auth_org_id) references public.organizations(auth_org_id) on delete cascade;
  end if;
end $$;

do $$
begin
  if exists (
    select 1 from pg_constraint where conname = 'organization_tenants_clerk_org_id_fkey'
  ) then
    alter table public.organization_tenants
      drop constraint organization_tenants_clerk_org_id_fkey;
  end if;
  if not exists (
    select 1 from pg_constraint where conname = 'organization_tenants_auth_org_id_fkey'
  ) then
    alter table public.organization_tenants
      add constraint organization_tenants_auth_org_id_fkey
      foreign key (auth_org_id) references public.organizations(auth_org_id) on delete cascade;
  end if;
end $$;

drop index if exists public.organization_members_org_idx;
drop index if exists public.organization_members_user_idx;
create index if not exists organization_members_auth_org_idx on public.organization_members (auth_org_id);
create index if not exists organization_members_auth_user_idx on public.organization_members (auth_user_id);

-- 6. Recreate policies that referenced the renamed columns / function name.

drop policy if exists organizations_org_access on public.organizations;
create policy organizations_org_access on public.organizations
  for all
  using (public.is_internal_user() or auth_org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or auth_org_id = public.current_auth_org_id());

drop policy if exists organization_members_org_access on public.organization_members;
create policy organization_members_org_access
  on public.organization_members
  for all
  using (
    public.is_internal_user() or auth_org_id = public.current_auth_org_id()
  )
  with check (
    public.is_internal_user() or auth_org_id = public.current_auth_org_id()
  );

drop policy if exists organization_tenants_org_access on public.organization_tenants;
create policy organization_tenants_org_access
  on public.organization_tenants
  for all
  using (
    public.is_internal_user() or auth_org_id = public.current_auth_org_id()
  )
  with check (
    public.is_internal_user() or auth_org_id = public.current_auth_org_id()
  );

drop policy if exists regulatory_publications_org_select on public.regulatory_publications;
create policy regulatory_publications_org_select on public.regulatory_publications
  for select
  using (
    public.is_internal_user()
    or exists (
      select 1
      from public.organizations organization
      where organization.auth_org_id = public.current_auth_org_id()
        and exists (
          select 1
          from jsonb_array_elements_text(
            coalesce(organization.regulatory_scope -> 'sources', '[]'::jsonb)
          ) source(value)
          where upper(source.value) = upper(regulatory_publications.source)
        )
    )
  );

-- 7. New control-plane / SaaS tables.

create table if not exists public.organization_invites (
  id text primary key default gen_random_uuid()::text,
  auth_org_id text not null references public.organizations(auth_org_id) on delete cascade,
  email text not null,
  role text not null default 'org_viewer',
  token text not null unique,
  expires_at timestamptz not null,
  invited_by text,
  status text not null default 'pending' check (status in ('pending', 'accepted', 'revoked', 'expired')),
  created_at timestamptz not null default now()
);

create index if not exists organization_invites_org_idx on public.organization_invites (auth_org_id);
create index if not exists organization_invites_email_idx on public.organization_invites (email);

alter table public.organization_invites enable row level security;
alter table public.organization_invites force row level security;

drop policy if exists organization_invites_org_access on public.organization_invites;
create policy organization_invites_org_access on public.organization_invites
  for all
  using (public.is_internal_user() or auth_org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or auth_org_id = public.current_auth_org_id());

create table if not exists public.organization_entitlements (
  auth_org_id text not null references public.organizations(auth_org_id) on delete cascade,
  product_code text not null,
  enabled boolean not null default true,
  primary key (auth_org_id, product_code)
);

alter table public.organization_entitlements enable row level security;
alter table public.organization_entitlements force row level security;

drop policy if exists organization_entitlements_org_access on public.organization_entitlements;
create policy organization_entitlements_org_access on public.organization_entitlements
  for all
  using (public.is_internal_user() or auth_org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or auth_org_id = public.current_auth_org_id());

-- Billing/subscription tables are written exclusively by the service role
-- (Stripe webhook handler); RLS keeps clients read-only over their own row.

create table if not exists public.billing_customers (
  auth_org_id text primary key references public.organizations(auth_org_id) on delete cascade,
  stripe_customer_id text not null unique,
  created_at timestamptz not null default now()
);

alter table public.billing_customers enable row level security;
alter table public.billing_customers force row level security;

drop policy if exists billing_customers_org_read on public.billing_customers;
create policy billing_customers_org_read on public.billing_customers
  for select
  using (public.is_internal_user() or auth_org_id = public.current_auth_org_id());

drop policy if exists billing_customers_service_write on public.billing_customers;
create policy billing_customers_service_write on public.billing_customers
  for all
  using (public.is_internal_user())
  with check (public.is_internal_user());

create table if not exists public.subscriptions (
  auth_org_id text primary key references public.organizations(auth_org_id) on delete cascade,
  stripe_subscription_id text,
  stripe_price_id text,
  plan_code text,
  status text not null default 'inactive',
  current_period_end timestamptz,
  cancel_at_period_end boolean not null default false,
  updated_at timestamptz not null default now()
);

alter table public.subscriptions enable row level security;
alter table public.subscriptions force row level security;

drop policy if exists subscriptions_org_read on public.subscriptions;
create policy subscriptions_org_read on public.subscriptions
  for select
  using (public.is_internal_user() or auth_org_id = public.current_auth_org_id());

drop policy if exists subscriptions_service_write on public.subscriptions;
create policy subscriptions_service_write on public.subscriptions
  for all
  using (public.is_internal_user())
  with check (public.is_internal_user());

create table if not exists public.marketing_contents (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  name text not null,
  storage_path text,
  status text not null default 'draft' check (status in ('draft', 'in_review', 'approved', 'rejected')),
  uploaded_by text,
  created_at timestamptz not null default now()
);

create index if not exists marketing_contents_org_idx on public.marketing_contents (org_id);

alter table public.marketing_contents enable row level security;
alter table public.marketing_contents force row level security;

drop policy if exists marketing_contents_org_access on public.marketing_contents;
create policy marketing_contents_org_access on public.marketing_contents
  for all
  using (public.is_internal_user() or org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or org_id = public.current_auth_org_id());

create table if not exists public.marketing_reviews (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  content_id text not null references public.marketing_contents(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'approved', 'rejected')),
  findings jsonb not null default '[]'::jsonb,
  decided_by text,
  decided_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists marketing_reviews_org_idx on public.marketing_reviews (org_id);
create index if not exists marketing_reviews_content_idx on public.marketing_reviews (content_id);

alter table public.marketing_reviews enable row level security;
alter table public.marketing_reviews force row level security;

drop policy if exists marketing_reviews_org_access on public.marketing_reviews;
create policy marketing_reviews_org_access on public.marketing_reviews
  for all
  using (public.is_internal_user() or org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or org_id = public.current_auth_org_id());

create table if not exists public.support_tickets (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  created_by text not null,
  subject text not null,
  body text not null,
  status text not null default 'open' check (status in ('open', 'pending', 'resolved', 'closed')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists support_tickets_org_idx on public.support_tickets (org_id);

alter table public.support_tickets enable row level security;
alter table public.support_tickets force row level security;

drop policy if exists support_tickets_org_access on public.support_tickets;
create policy support_tickets_org_access on public.support_tickets
  for all
  using (public.is_internal_user() or org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or org_id = public.current_auth_org_id());

create table if not exists public.usage_counters (
  org_id text not null,
  day date not null,
  metric text not null,
  count bigint not null default 0,
  primary key (org_id, day, metric)
);

create index if not exists usage_counters_org_day_idx on public.usage_counters (org_id, day desc);

alter table public.usage_counters enable row level security;
alter table public.usage_counters force row level security;

drop policy if exists usage_counters_org_access on public.usage_counters;
create policy usage_counters_org_access on public.usage_counters
  for all
  using (public.is_internal_user() or org_id = public.current_auth_org_id())
  with check (public.is_internal_user() or org_id = public.current_auth_org_id());
