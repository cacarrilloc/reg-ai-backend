-- Isolated tenant projects (public schema only): vectors staged during an embedding-model switch.
-- Re-embed job progress stays on the platform control plane.

create table if not exists public.ai_reembed_vectors (
    chunk_id text primary key,
    org_id text not null,
    embedding vector(3072) not null,
    created_at timestamp without time zone not null default (now() at time zone 'utc')
);

create index if not exists ai_reembed_vectors_org_idx on public.ai_reembed_vectors (org_id);

alter table public.ai_reembed_vectors enable row level security;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.ai_reembed_vectors from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.ai_reembed_vectors from authenticated;
    end if;
end
$$;
