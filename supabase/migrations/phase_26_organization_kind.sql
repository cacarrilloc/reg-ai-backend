-- Phase 26: distinguish internal (IPX) orgs from external customer orgs.
-- Internal orgs bind to the platform Supabase project for this deploy env.
-- External orgs may receive a dedicated Supabase project via provisioning.

alter table public.organizations
  add column if not exists kind text not null default 'external';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'organizations_kind_check'
      and conrelid = 'public.organizations'::regclass
  ) then
    alter table public.organizations
      add constraint organizations_kind_check
      check (kind in ('internal', 'external'));
  end if;
end $$;

create index if not exists organizations_kind_idx
  on public.organizations (kind);
