-- Split government regulations out of policies.
-- policies = internal firm documents (uploaded / WSP / marketing shells)
-- regulations = external / gov REGULATORY_REFERENCE rows, including the four marketing rules.

create or replace function public.apply_phase_42_regulations(target_schema text)
returns void
language plpgsql
as $$
declare
    leftover boolean;
    constraint_exists boolean;
begin
    if target_schema is null or target_schema !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' then
        raise exception 'refusing to migrate invalid schema %', target_schema;
    end if;

    execute format($ddl$
        create table if not exists %I.regulations (
            id text primary key,
            org_id text not null,
            name text not null,
            type text not null default 'REGULATORY_REFERENCE',
            version integer not null default 1,
            status text not null default 'ACTIVE',
            file_url text,
            source text,
            rule_code text,
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now()
        )
    $ddl$, target_schema);

    execute format(
        'create unique index if not exists regulations_org_name_version_key on %I.regulations (org_id, name, version)',
        target_schema
    );
    execute format(
        'create unique index if not exists regulations_org_rule_code_key on %I.regulations (org_id, rule_code) where rule_code is not null',
        target_schema
    );
    execute format(
        'create index if not exists regulations_org_id_idx on %I.regulations (org_id)',
        target_schema
    );
    execute format(
        'create index if not exists regulations_org_source_idx on %I.regulations (org_id, source)',
        target_schema
    );

    execute format($ddl$
        insert into %I.regulations (
            id, org_id, name, type, version, status, file_url, source, rule_code, created_at, updated_at
        )
        select
            p.id,
            p.org_id,
            p.name,
            'REGULATORY_REFERENCE',
            p.version,
            p.status,
            p.file_url,
            case
                when upper(p.name) like 'SEC%%' then 'SEC'
                when upper(p.name) like 'FINRA%%' then 'FINRA'
                when upper(p.name) like 'CFTC%%' then 'CFTC'
                else null
            end,
            null,
            p.created_at,
            p.updated_at
        from %I.policies p
        where upper(p.type) = 'REGULATORY_REFERENCE'
        on conflict (id) do nothing
    $ddl$, target_schema, target_schema);

    execute format(
        'alter table %I.document_chunks add column if not exists regulation_id text',
        target_schema
    );

    if not exists (
        select 1
        from pg_constraint con
        join pg_class rel on rel.oid = con.conrelid
        join pg_namespace nsp on nsp.oid = rel.relnamespace
        where nsp.nspname = target_schema
          and rel.relname = 'document_chunks'
          and con.conname = 'document_chunks_regulation_id_fkey'
    ) then
        execute format(
            'alter table %I.document_chunks add constraint document_chunks_regulation_id_fkey foreign key (regulation_id) references %I.regulations(id) on delete cascade',
            target_schema,
            target_schema
        );
    end if;

    execute format(
        'create unique index if not exists document_chunks_org_regulation_chunk_key on %I.document_chunks (org_id, regulation_id, chunk_index) where regulation_id is not null',
        target_schema
    );

    -- Drop the old XOR so rows can briefly have both policy_id and regulation_id.
    execute format(
        'alter table %I.document_chunks drop constraint if exists document_chunks_check',
        target_schema
    );
    execute format(
        'alter table %I.document_chunks drop constraint if exists document_chunks_owner_check',
        target_schema
    );

    execute format($ddl$
        update %I.document_chunks as chunk
        set regulation_id = chunk.policy_id
        from %I.regulations as regulation
        where chunk.policy_id = regulation.id
          and chunk.regulation_id is null
    $ddl$, target_schema, target_schema);

    execute format($ddl$
        update %I.document_chunks
        set policy_id = null
        where regulation_id is not null
          and policy_id is not null
    $ddl$, target_schema);

    execute format($ddl$
        alter table %I.document_chunks
            add constraint document_chunks_owner_check
            check (num_nonnulls(policy_id, publication_id, context_document_id, regulation_id) = 1)
    $ddl$, target_schema);

    if to_regclass(format('%I.compliance_history', target_schema)) is not null then
        execute format($ddl$
            update %I.compliance_history
            set policy_id = null
            where policy_id in (select id from %I.regulations)
        $ddl$, target_schema, target_schema);
    end if;

    if to_regclass(format('%I.audit_logs', target_schema)) is not null then
        execute format($ddl$
            update %I.audit_logs
            set policy_id = null
            where policy_id in (select id from %I.regulations)
        $ddl$, target_schema, target_schema);
    end if;

    execute format($ddl$
        delete from %I.policies as policy
        where upper(policy.type) = 'REGULATORY_REFERENCE'
          and not exists (
              select 1 from %I.gap_analyses gap where gap.policy_id = policy.id
          )
          and not exists (
              select 1 from %I.policy_revisions revision where revision.policy_id = policy.id
          )
    $ddl$, target_schema, target_schema, target_schema);

    execute format(
        'select exists(select 1 from %I.policies where upper(type) = ''REGULATORY_REFERENCE'')',
        target_schema
    ) into leftover;

    select exists (
        select 1
        from pg_constraint con
        join pg_class rel on rel.oid = con.conrelid
        join pg_namespace nsp on nsp.oid = rel.relnamespace
        where nsp.nspname = target_schema
          and rel.relname = 'policies'
          and con.conname = 'policies_internal_type_check'
    ) into constraint_exists;

    if not leftover and not constraint_exists then
        execute format(
            'alter table %I.policies add constraint policies_internal_type_check check (upper(type) <> ''REGULATORY_REFERENCE'')',
            target_schema
        );
    end if;

    execute format($ddl$
        insert into %I.regulations (
            id, org_id, name, type, version, status, file_url, source, rule_code, created_at, updated_at
        )
        select
            'mkt-' || md5(orgs.org_id || ':' || catalog.rule_code),
            orgs.org_id,
            catalog.name,
            'REGULATORY_REFERENCE',
            1,
            'ACTIVE',
            null,
            catalog.source,
            catalog.rule_code,
            now(),
            now()
        from (
            select distinct org_id
            from (
                select org_id from %I.policies
                union
                select org_id from %I.regulations
            ) listed
            where org_id is not null and btrim(org_id) <> ''
        ) orgs
        cross join (
            values
                ('SEC', '206(4)-1', 'SEC: Rule 206(4)-1, the Marketing Rule'),
                ('SEC', '204-2', 'SEC: Rule 204-2, recordkeeping'),
                ('FINRA', '2210', 'FINRA: Rule 2210, communications with the public'),
                ('FINRA', '2211-2216', 'FINRA: Rules 2211–2216')
        ) as catalog(source, rule_code, name)
        on conflict do nothing
    $ddl$, target_schema, target_schema, target_schema);

    if to_regclass('public.organizations') is not null and target_schema = 'public' then
        execute $ddl$
            insert into public.regulations (
                id, org_id, name, type, version, status, file_url, source, rule_code, created_at, updated_at
            )
            select
                'mkt-' || md5(org.auth_org_id || ':' || catalog.rule_code),
                org.auth_org_id,
                catalog.name,
                'REGULATORY_REFERENCE',
                1,
                'ACTIVE',
                null,
                catalog.source,
                catalog.rule_code,
                now(),
                now()
            from public.organizations org
            cross join (
                values
                    ('SEC', '206(4)-1', 'SEC: Rule 206(4)-1, the Marketing Rule'),
                    ('SEC', '204-2', 'SEC: Rule 204-2, recordkeeping'),
                    ('FINRA', '2210', 'FINRA: Rule 2210, communications with the public'),
                    ('FINRA', '2211-2216', 'FINRA: Rules 2211–2216')
            ) as catalog(source, rule_code, name)
            where org.auth_org_id is not null and btrim(org.auth_org_id) <> ''
            on conflict do nothing
        $ddl$;
    end if;

    if to_regclass('public.organization_tenants') is not null then
        execute format($ddl$
            insert into %I.regulations (
                id, org_id, name, type, version, status, file_url, source, rule_code, created_at, updated_at
            )
            select
                'mkt-' || md5(tenant.auth_org_id || ':' || catalog.rule_code),
                tenant.auth_org_id,
                catalog.name,
                'REGULATORY_REFERENCE',
                1,
                'ACTIVE',
                null,
                catalog.source,
                catalog.rule_code,
                now(),
                now()
            from public.organization_tenants tenant
            cross join (
                values
                    ('SEC', '206(4)-1', 'SEC: Rule 206(4)-1, the Marketing Rule'),
                    ('SEC', '204-2', 'SEC: Rule 204-2, recordkeeping'),
                    ('FINRA', '2210', 'FINRA: Rule 2210, communications with the public'),
                    ('FINRA', '2211-2216', 'FINRA: Rules 2211–2216')
            ) as catalog(source, rule_code, name)
            where tenant.schema_name = %L
              and tenant.auth_org_id is not null
              and btrim(tenant.auth_org_id) <> ''
            on conflict do nothing
        $ddl$, target_schema, target_schema);
    end if;

    execute format('alter table %I.regulations enable row level security', target_schema);
    execute format('alter table %I.regulations force row level security', target_schema);
    execute format('drop policy if exists regulations_org_access on %I.regulations', target_schema);
    execute format($ddl$
        create policy regulations_org_access on %I.regulations
          for all
          using (public.is_internal_user() or org_id = public.current_clerk_org_id())
          with check (public.is_internal_user() or org_id = public.current_clerk_org_id())
    $ddl$, target_schema);
end;
$$;

select public.apply_phase_42_regulations('public');

do $$
declare
    tenant_schema text;
begin
    if to_regclass('public.organization_tenants') is null then
        return;
    end if;
    for tenant_schema in
        select schema_name
        from public.organization_tenants
        where isolation_mode = 'schema'
          and status = 'ready'
          and schema_name ~ '^(?:[a-z0-9_]{1,55}_tenant|tenant_[a-z0-9_]{1,55})$'
    loop
        perform public.apply_phase_42_regulations(tenant_schema);
    end loop;
end
$$;
