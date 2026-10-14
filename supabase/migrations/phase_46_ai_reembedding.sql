-- phase_46: switch an org's embedding model by re-embedding its documents.
--   organization_ai_reembed_jobs: control plane (public only), progress of one rebuild per org.
--   ai_reembed_vectors: new-model vectors staged per chunk until the rebuild swaps them into
--   document_chunks. Created in every org tenant schema; the public copy is the clone
--   template and the data plane for orgs that are not schema-isolated.

create table if not exists public.organization_ai_reembed_jobs (
    id text primary key,
    auth_org_id text not null,
    status text not null check (status in ('running', 'swapped', 'completed', 'failed')),
    target jsonb not null,
    total_chunks integer not null default 0,
    embedded_chunks integer not null default 0,
    error text,
    created_by text,
    started_at timestamp without time zone not null default (now() at time zone 'utc'),
    swapped_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone not null default (now() at time zone 'utc')
);

create index if not exists organization_ai_reembed_jobs_org_idx
    on public.organization_ai_reembed_jobs (auth_org_id, started_at desc);

-- At most one rebuild in flight per org.
create unique index if not exists organization_ai_reembed_jobs_one_active
    on public.organization_ai_reembed_jobs (auth_org_id)
    where status in ('running', 'swapped');

alter table public.organization_ai_reembed_jobs enable row level security;

create table if not exists public.ai_reembed_vectors (
    chunk_id text primary key,
    org_id text not null,
    embedding vector(3072) not null,
    created_at timestamp without time zone not null default (now() at time zone 'utc')
);

create index if not exists ai_reembed_vectors_org_idx on public.ai_reembed_vectors (org_id);

create or replace function public.apply_phase_46_ai_reembed_vectors(target_schema text)
returns void
language plpgsql
as $$
declare
    api_role text;
begin
    if target_schema is null or target_schema !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' then
        raise exception 'refusing to migrate invalid schema %', target_schema;
    end if;

    if target_schema <> 'public' then
        execute format(
            'create table if not exists %I.ai_reembed_vectors (like public.ai_reembed_vectors including all)',
            target_schema
        );
    end if;
    execute format('alter table %I.ai_reembed_vectors enable row level security', target_schema);
    execute format(
        'comment on table %I.ai_reembed_vectors is %L',
        target_schema,
        'New-model vectors staged during an embedding-model switch; emptied when the switch completes.'
    );

    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on %I.ai_reembed_vectors from %I', target_schema, api_role);
        end if;
    end loop;
end;
$$;

select public.apply_phase_46_ai_reembed_vectors('public');

do $$
declare
    tenant_schema text;
begin
    for tenant_schema in
        select schema_name
        from information_schema.schemata
        where schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
    loop
        perform public.apply_phase_46_ai_reembed_vectors(tenant_schema);
    end loop;
end
$$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.organization_ai_reembed_jobs from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.organization_ai_reembed_jobs from authenticated;
    end if;
end
$$;

comment on table public.organization_ai_reembed_jobs is
  'Embedding-model switches: documents are re-embedded, then the new model replaces the old one.';
