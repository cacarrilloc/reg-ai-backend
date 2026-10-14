-- Mirror of alembic phase_2_ingestion for tenant project bootstrap.
create unique index if not exists policies_org_name_version_key
  on public.policies (org_id, name, version);

create index if not exists document_chunks_org_policy_chunk_idx
  on public.document_chunks (org_id, policy_id, chunk_index)
  where policy_id is not null;

create index if not exists context_documents_org_type_created_idx
  on public.context_documents (org_id, document_type, created_at desc);
