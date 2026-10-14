-- Store per-tenant Supabase API credentials alongside database_url.

alter table public.organization_tenants
  add column if not exists supabase_url text;

alter table public.organization_tenants
  add column if not exists supabase_service_role_key text;
