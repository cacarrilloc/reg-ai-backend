alter table public.document_chunks add column if not exists policy_name text;

create index if not exists document_chunks_policy_name_idx
  on public.document_chunks (policy_name);

create index if not exists document_chunks_org_policy_name_idx
  on public.document_chunks (org_id, policy_name);

update public.document_chunks dc
set policy_name = p.name
from public.policies p
where dc.policy_id = p.id
  and dc.policy_name is null;

update public.document_chunks dc
set policy_name = upper(p.name)
from public.policies p
where dc.policy_id = p.id
  and dc.document_type = 'WSP'
  and upper(p.name) in ('FINRA', 'SEC', 'CFTC')
  and dc.policy_name is distinct from upper(p.name);

update public.document_chunks dc
set policy_name = (
  case
    when p.name ilike '%FINRA%' then 'FINRA'
    when p.name ilike '%CFTC%' then 'CFTC'
    when p.name ilike '%SEC%' then 'SEC'
    else dc.policy_name
  end
)
from public.policies p
where dc.policy_id = p.id
  and dc.document_type = 'REGULATORY_REFERENCE';

update public.document_chunks
set policy_name = upper(metadata ->> 'source')
where publication_id is not null
  and policy_name is null
  and metadata ->> 'source' is not null;
