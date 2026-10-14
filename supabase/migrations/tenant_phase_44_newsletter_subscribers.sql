-- Isolated tenant projects (public schema only). Public signups stay on the
-- platform general_newsletter_subscribers table, not on tenant projects.

create table if not exists public.newsletter_subscribers (
    id text primary key,
    email text not null,
    auth_org_id text not null,
    invited_by text,
    source text not null default 'invite',
    subscribed_at timestamp without time zone not null default (now() at time zone 'utc'),
    constraint newsletter_subscribers_org_email_key unique (auth_org_id, email)
);

create index if not exists newsletter_subscribers_org_idx
  on public.newsletter_subscribers (auth_org_id);
create index if not exists newsletter_subscribers_subscribed_at_idx
  on public.newsletter_subscribers (subscribed_at desc);
