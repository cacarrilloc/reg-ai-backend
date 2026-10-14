-- Phase 28: allow hyphens in usernames (e.g. reg-ai-admin).

alter table public.user_profiles
  drop constraint if exists user_profiles_username_format;

alter table public.user_profiles
  add constraint user_profiles_username_format check (
    username ~ '^[a-z0-9][a-z0-9_-]{1,30}[a-z0-9]$'
  );
