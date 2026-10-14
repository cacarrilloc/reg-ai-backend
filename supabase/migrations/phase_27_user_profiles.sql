-- Phase 27: user profiles with 1:1 username <-> email uniqueness.

create table if not exists public.user_profiles (
  auth_user_id text primary key,
  username text not null,
  email text not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint user_profiles_username_format check (
    username ~ '^[a-z0-9_]{3,32}$'
  )
);

create unique index if not exists user_profiles_username_key
  on public.user_profiles (username);

create unique index if not exists user_profiles_email_key
  on public.user_profiles (lower(email));

alter table public.user_profiles enable row level security;
alter table public.user_profiles force row level security;

drop policy if exists user_profiles_self_access on public.user_profiles;
create policy user_profiles_self_access on public.user_profiles
  for all
  using (
    public.is_internal_user()
    or auth_user_id = coalesce(auth.jwt() ->> 'sub', '')
  )
  with check (
    public.is_internal_user()
    or auth_user_id = coalesce(auth.jwt() ->> 'sub', '')
  );
