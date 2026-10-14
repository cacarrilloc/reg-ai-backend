-- phase_44: org newsletter_subscribers (cloned) + public general_newsletter_subscribers

create or replace function public.apply_phase_44_newsletter_subscribers(target_schema text)
returns void
language plpgsql
as $$
begin
    if target_schema is null or target_schema !~ '^[a-zA-Z_][a-zA-Z0-9_]*$' then
        raise exception 'refusing to migrate invalid schema %', target_schema;
    end if;

    execute format($ddl$
        create table if not exists %I.newsletter_subscribers (
            id text primary key,
            email text not null,
            auth_org_id text not null,
            invited_by text,
            source text not null default 'invite',
            subscribed_at timestamp without time zone not null default (now() at time zone 'utc'),
            constraint newsletter_subscribers_org_email_key unique (auth_org_id, email)
        )
    $ddl$, target_schema);

    execute format(
        'create index if not exists newsletter_subscribers_org_idx on %I.newsletter_subscribers (auth_org_id)',
        target_schema
    );
    execute format(
        'create index if not exists newsletter_subscribers_subscribed_at_idx on %I.newsletter_subscribers (subscribed_at desc)',
        target_schema
    );

    execute format(
        'comment on table %I.newsletter_subscribers is %L',
        target_schema,
        'Reg-AI Newsletter recipients invited from this organization.'
    );
end;
$$;

select public.apply_phase_44_newsletter_subscribers('public');

create table if not exists public.general_newsletter_subscribers (
    id text primary key,
    email text not null,
    subscribed_at timestamp without time zone not null default (now() at time zone 'utc'),
    constraint general_newsletter_subscribers_email_key unique (email)
);

create index if not exists general_newsletter_subscribers_subscribed_at_idx
  on public.general_newsletter_subscribers (subscribed_at desc);

comment on table public.general_newsletter_subscribers is
  'Public Reg-AI Newsletter signups that are not tied to an organization.';

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
        perform public.apply_phase_44_newsletter_subscribers(tenant_schema);
    end loop;
end
$$;
