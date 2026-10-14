alter table public.document_chunks add column if not exists document_type text;

create index if not exists document_chunks_document_type_idx
  on public.document_chunks (document_type);

create index if not exists document_chunks_org_document_type_idx
  on public.document_chunks (org_id, document_type);

update public.document_chunks dc
set document_type = upper(p.type)
from public.policies p
where dc.policy_id = p.id
  and dc.document_type is null;

update public.document_chunks
set document_type = 'REGULATORY_REFERENCE'
where document_type is null
  and (
    metadata ->> 'reference_type' = 'REGULATORY_REFERENCE'
    or metadata ? 'source'
  )
  and policy_id is not null;

update public.document_chunks
set document_type = 'REGULATORY_PUBLICATION'
where publication_id is not null
  and document_type is null;

update public.document_chunks
set document_type = 'CONTEXT_DOCUMENT'
where context_document_id is not null
  and document_type is null;

update public.document_chunks
set document_type = 'WSP'
where policy_id is not null
  and document_type is null;
