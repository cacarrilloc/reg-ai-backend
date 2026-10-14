create unique index if not exists regulatory_publications_source_url_key
  on public.regulatory_publications (source, url);

create index if not exists regulatory_publications_published_at_idx
  on public.regulatory_publications (published_at desc nulls last, created_at desc);

create table if not exists public.publication_event_deliveries (
  id text primary key default gen_random_uuid()::text,
  publication_id text not null references public.regulatory_publications(id) on delete cascade,
  org_id text not null,
  event_id text,
  sent_at timestamptz,
  created_at timestamptz not null default now(),
  unique (publication_id, org_id)
);

create index if not exists publication_event_deliveries_pending_idx
  on public.publication_event_deliveries (created_at) where sent_at is null;

alter table public.publication_event_deliveries enable row level security;

drop policy if exists publication_deliveries_internal_access on public.publication_event_deliveries;
create policy publication_deliveries_internal_access on public.publication_event_deliveries
  for all
  using (public.is_internal_user())
  with check (public.is_internal_user());
