# Encryption Policy

**Owner:** Security / Engineering  
**Applies to:** Reg-AI production and staging data stores, APIs, and operator connections  
**Review cadence:** At least annually  

## 1. Purpose

Define encryption expectations for data in transit and at rest under the SOC 2 shared-responsibility model.

## 2. Data in transit

1. Public application and API endpoints are served only over **HTTPS/TLS** (Vercel, Railway).
2. Supabase Auth, Storage, and API clients use TLS.
3. Direct Postgres connections must use TLS. Prefer `sslmode=require` (or stronger) on `DATABASE_URL`. Supabase hosts are treated as TLS-required by application health posture checks.
4. HSTS is enabled on staging/production HTTP responses via security headers.

## 3. Data at rest

1. Hosted Postgres and Storage rely on **platform disk encryption** provided by Supabase.
2. Application secrets (API keys, JWTs, webhook secrets) are stored in Railway/Vercel environment configuration, not in source control or browser bundles.
3. Object storage paths are organization-prefixed; buckets are private.

## 4. Key management

1. Platform providers manage disk encryption keys.
2. Application secrets are rotated on suspected compromise and when operators leave.
3. CI uses short-lived GitHub Actions runners; repository secrets are limited to build needs.

## 5. Verification / evidence

| Control | How we verify |
| --- | --- |
| HTTPS edge | Platform TLS certs + HSTS headers |
| DB TLS intent | `/health` field `database_tls_requested` |
| MFA paired with access | `/health` MFA flags + frontend MFA gate |
| Vendor at-rest encryption | Supabase / host trust center attestations |

Operator checklist:

1. Confirm Supabase “Enforce SSL” for direct DB clients.
2. Confirm production `DATABASE_URL` uses TLS (`sslmode=require` or Supabase pooler URL).
3. Confirm no secret values in frontend `NEXT_PUBLIC_*` variables except publishable keys.
4. Retain annual vendor encryption attestations with the vendor management review.

## 6. Related documents

- `docs/compliance-foundations.md`
- `docs/security-hardening.md`
- `docs/policies/vendor-management.md`
