create extension if not exists pgcrypto;
create extension if not exists vector;

create or replace function public.current_clerk_org_id()
returns text
language sql
stable
as $$
  select coalesce(
    nullif(auth.jwt() ->> 'org_id', ''),
    nullif(auth.jwt() ->> 'orgId', ''),
    nullif(auth.jwt() #>> '{o,id}', '')
  );
$$;

create or replace function public.current_internal_role()
returns text
language sql
stable
as $$
  select replace(
    lower(
      coalesce(
        auth.jwt() #>> '{metadata,role}',
        auth.jwt() #>> '{public_metadata,role}',
        auth.jwt() ->> 'role',
        auth.jwt() ->> 'org_role',
        ''
      )
    ),
    ' ',
    '_'
  );
$$;

create or replace function public.is_internal_user()
returns boolean
language sql
stable
as $$
  select public.current_internal_role() = any (
    array['master_user', 'developer_user', 'product_manager', 'admin_user']
  );
$$;

create table if not exists public.organizations (
  id text primary key default gen_random_uuid()::text,
  clerk_org_id text not null unique,
  name text not null,
  regulatory_scope jsonb not null default '{}'::jsonb,
  cco_user_id text,
  approval_chain jsonb not null default '{}'::jsonb,
  escalation_days integer not null default 7 check (escalation_days between 1 and 90),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.policies (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  name text not null,
  type text not null default 'uploaded',
  version integer not null default 1,
  status text not null default 'ACTIVE',
  file_url text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.regulatory_publications (
  id text primary key default gen_random_uuid()::text,
  source text not null,
  title text not null,
  url text not null,
  published_at timestamptz,
  raw_text text,
  processed boolean not null default false,
  embedding_chunk_ids jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.context_documents (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  document_type text not null,
  title text not null,
  file_url text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.document_chunks (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  policy_id text references public.policies(id) on delete cascade,
  publication_id text references public.regulatory_publications(id) on delete cascade,
  context_document_id text references public.context_documents(id) on delete cascade,
  chunk_index integer not null default 0,
  content text not null,
  embedding vector(3072),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (
    num_nonnulls(policy_id, publication_id, context_document_id) = 1
  )
);

create table if not exists public.business_changes (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  change_type text not null,
  description text not null,
  flagged_by text not null,
  flagged_at timestamptz not null default now(),
  triggers_review boolean not null default false
);

create table if not exists public.gap_analyses (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  publication_id text references public.regulatory_publications(id) on delete set null,
  business_change_id text references public.business_changes(id) on delete set null,
  policy_id text not null references public.policies(id) on delete cascade,
  impact_score text not null,
  gap_summary text not null,
  regulatory_citations jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.policy_revisions (
  id text primary key default gen_random_uuid()::text,
  gap_analysis_id text not null references public.gap_analyses(id) on delete cascade,
  redline_file_url text,
  status text not null default 'PENDING',
  reviewer_id text,
  reviewed_at timestamptz,
  approved_file_url text,
  created_at timestamptz not null default now()
);

create table if not exists public.audit_logs (
  id text primary key default gen_random_uuid()::text,
  org_id text not null,
  event_type text not null,
  actor_id text not null,
  timestamp timestamptz not null default now(),
  publication_id text,
  policy_id text,
  revision_id text,
  metadata jsonb not null default '{}'::jsonb
);

create index if not exists organizations_clerk_org_id_idx on public.organizations(clerk_org_id);
create index if not exists policies_org_id_idx on public.policies(org_id);
create index if not exists regulatory_publications_source_idx on public.regulatory_publications(source);
create index if not exists context_documents_org_id_idx on public.context_documents(org_id);
create index if not exists document_chunks_org_id_idx on public.document_chunks(org_id);
create index if not exists document_chunks_policy_id_idx on public.document_chunks(policy_id);
create index if not exists document_chunks_context_document_id_idx on public.document_chunks(context_document_id);
create index if not exists document_chunks_embedding_cosine_idx
  on public.document_chunks using hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
create index if not exists business_changes_org_id_idx on public.business_changes(org_id);
create index if not exists gap_analyses_org_id_idx on public.gap_analyses(org_id);
create index if not exists audit_logs_org_id_idx on public.audit_logs(org_id);

alter table public.organizations enable row level security;
alter table public.policies enable row level security;
alter table public.regulatory_publications enable row level security;
alter table public.context_documents enable row level security;
alter table public.document_chunks enable row level security;
alter table public.business_changes enable row level security;
alter table public.gap_analyses enable row level security;
alter table public.policy_revisions enable row level security;
alter table public.audit_logs enable row level security;

drop policy if exists organizations_org_access on public.organizations;
create policy organizations_org_access on public.organizations
  for all
  using (public.is_internal_user() or clerk_org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or clerk_org_id = public.current_clerk_org_id());

drop policy if exists policies_org_access on public.policies;
create policy policies_org_access on public.policies
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists publications_read_authenticated on public.regulatory_publications;
create policy publications_read_authenticated on public.regulatory_publications
  for select
  using (auth.role() = 'authenticated' or public.is_internal_user());

drop policy if exists publications_internal_write on public.regulatory_publications;
create policy publications_internal_write on public.regulatory_publications
  for all
  using (public.is_internal_user())
  with check (public.is_internal_user());

drop policy if exists context_documents_org_access on public.context_documents;
create policy context_documents_org_access on public.context_documents
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists document_chunks_org_access on public.document_chunks;
create policy document_chunks_org_access on public.document_chunks
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists business_changes_org_access on public.business_changes;
create policy business_changes_org_access on public.business_changes
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists gap_analyses_org_access on public.gap_analyses;
create policy gap_analyses_org_access on public.gap_analyses
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

drop policy if exists policy_revisions_org_access on public.policy_revisions;
create policy policy_revisions_org_access on public.policy_revisions
  for all
  using (
    public.is_internal_user()
    or exists (
      select 1
      from public.gap_analyses
      where gap_analyses.id = policy_revisions.gap_analysis_id
        and gap_analyses.org_id = public.current_clerk_org_id()
    )
  )
  with check (
    public.is_internal_user()
    or exists (
      select 1
      from public.gap_analyses
      where gap_analyses.id = policy_revisions.gap_analysis_id
        and gap_analyses.org_id = public.current_clerk_org_id()
    )
  );

drop policy if exists audit_logs_org_access on public.audit_logs;
create policy audit_logs_org_access on public.audit_logs
  for all
  using (public.is_internal_user() or org_id = public.current_clerk_org_id())
  with check (public.is_internal_user() or org_id = public.current_clerk_org_id());

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('policy-documents', 'policy-documents', false, 52428800, array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain'
  ]),
  ('policy-redlines', 'policy-redlines', false, 52428800, array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  ]),
  ('approved-policies', 'approved-policies', false, 52428800, array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
  ]),
  ('context-documents', 'context-documents', false, 52428800, array[
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain',
    'text/csv',
    'application/json'
  ])
on conflict (id) do update
set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists org_storage_select on storage.objects;
create policy org_storage_select on storage.objects
  for select
  to authenticated
  using (
    bucket_id in ('policy-documents', 'policy-redlines', 'approved-policies', 'context-documents')
    and (public.is_internal_user() or (storage.foldername(name))[1] = public.current_clerk_org_id())
  );

drop policy if exists org_storage_insert on storage.objects;
create policy org_storage_insert on storage.objects
  for insert
  to authenticated
  with check (
    bucket_id in ('policy-documents', 'policy-redlines', 'approved-policies', 'context-documents')
    and (public.is_internal_user() or (storage.foldername(name))[1] = public.current_clerk_org_id())
  );

drop policy if exists org_storage_update on storage.objects;
create policy org_storage_update on storage.objects
  for update
  to authenticated
  using (
    bucket_id in ('policy-documents', 'policy-redlines', 'approved-policies', 'context-documents')
    and (public.is_internal_user() or (storage.foldername(name))[1] = public.current_clerk_org_id())
  )
  with check (
    bucket_id in ('policy-documents', 'policy-redlines', 'approved-policies', 'context-documents')
    and (public.is_internal_user() or (storage.foldername(name))[1] = public.current_clerk_org_id())
  );
