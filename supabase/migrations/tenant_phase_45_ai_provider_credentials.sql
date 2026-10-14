-- Isolated tenant projects (public schema only): per-org LLM provider keys.
-- Org AI settings stay on the platform control plane.

create table if not exists public.ai_provider_credentials (
    id text primary key,
    org_id text not null,
    provider text not null,
    provider_name text not null,
    auth_key_ciphertext text not null,
    key_hint text not null,
    api_base text,
    enabled_models jsonb not null default '[]'::jsonb,
    updated_by text,
    created_at timestamp without time zone not null default (now() at time zone 'utc'),
    updated_at timestamp without time zone not null default (now() at time zone 'utc'),
    constraint ai_provider_credentials_org_provider_key unique (org_id, provider)
);

create index if not exists ai_provider_credentials_org_idx
  on public.ai_provider_credentials (org_id);

alter table public.ai_provider_credentials enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.ai_provider_credentials from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.ai_provider_credentials from authenticated;
    end if;
end
$$;
