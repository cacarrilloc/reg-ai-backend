-- Mirror of alembic phase_16_policy_type_uploaded for tenant project bootstrap.
update public.policies
set type = 'uploaded'
where type = 'WSP';

alter table public.policies
  alter column type set default 'uploaded';

update public.document_chunks
set document_type = 'uploaded'
where document_type = 'WSP';
