create unique index if not exists document_chunks_org_policy_chunk_key
  on public.document_chunks (org_id, policy_id, chunk_index)
  where policy_id is not null;
