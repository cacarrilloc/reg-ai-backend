-- phase_37: organization logo branding

alter table public.organizations
  add column if not exists logo_url text;

-- Private/public storage URI (storage://organization-logos/...) or https URL.
comment on column public.organizations.logo_url is
  'Organization logo stored as storage:// URI or absolute https URL';
