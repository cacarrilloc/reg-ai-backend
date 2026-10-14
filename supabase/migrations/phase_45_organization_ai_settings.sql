-- phase_45: BYOK model layer.
--   organization_ai_settings: control plane (public only), holds no secrets.
--   ai_provider_credentials: one encrypted key per LLM company per org, created in every
--   org tenant schema. The public copy is only the clone template for new tenant schemas
--   and the data plane for orgs that are not schema-isolated.

create table if not exists public.organization_ai_settings (
    auth_org_id text primary key,
    mode text not null default 'platform' check (mode in ('platform', 'byok')),
    generation_provider text,
    generation_model text,
    generation_api_base text,
    embedding_provider text not null,
    embedding_model text not null,
    embedding_api_base text,
    embedding_dimensions integer not null check (embedding_dimensions > 0),
    updated_by text,
    created_at timestamp without time zone not null default (now() at time zone 'utc'),
    updated_at timestamp without time zone not null default (now() at time zone 'utc'),
    constraint organization_ai_settings_byok_generation check (
        mode = 'platform' or (generation_provider is not null and generation_model is not null)
    )
);

alter table public.organization_ai_settings enable row level security;

create or replace function public.apply_phase_45_ai_provider_credentials(target_schema text)
returns void
language plpgsql
as $$
declare
    api_role text;
begin
    if target_schema is null or target_schema !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' then
        raise exception 'refusing to migrate invalid schema %', target_schema;
    end if;

    execute format($ddl$
        create table if not exists %I.ai_provider_credentials (
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
        )
    $ddl$, target_schema);

    execute format(
        'create index if not exists ai_provider_credentials_org_idx on %I.ai_provider_credentials (org_id)',
        target_schema
    );
    execute format('alter table %I.ai_provider_credentials enable row level security', target_schema);
    execute format(
        'comment on table %I.ai_provider_credentials is %L',
        target_schema,
        'Per-org LLM provider keys. AES-256-GCM ciphertext bound to org:provider; plaintext is never stored.'
    );

    -- Backend service role only: no PostgREST policies, so anon/authenticated see nothing.
    foreach api_role in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = api_role) then
            execute format('revoke all on %I.ai_provider_credentials from %I', target_schema, api_role);
        end if;
    end loop;
end;
$$;

select public.apply_phase_45_ai_provider_credentials('public');

do $$
declare
    tenant_schema text;
begin
    for tenant_schema in
        select schema_name
        from information_schema.schemata
        where schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
    loop
        perform public.apply_phase_45_ai_provider_credentials(tenant_schema);
    end loop;
end
$$;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on public.organization_ai_settings from anon;
    end if;
    if exists (select 1 from pg_roles where rolname = 'authenticated') then
        revoke all on public.organization_ai_settings from authenticated;
    end if;
end
$$;

comment on table public.organization_ai_settings is
  'Per-organization AI provider/model selection (BYOK). No row means platform defaults.';
