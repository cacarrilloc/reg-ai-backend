# Compliance foundations (launch pack)

This document maps Reg AI’s Aug 15 launch posture to encryption, tenancy,
auditability, and shared-responsibility compliance programs (GDPR / HIPAA / SOC 2).
It is **not** a certification attestation.

## Architecture summary

| Layer | Technology | Isolation / control |
| --- | --- | --- |
| Identity | Supabase Auth (control-plane project) | Users, MFA (TOTP), optional SAML SSO |
| Tenancy | App-owned `organizations` + `organization_members` | JWT claim `org_id` + FastAPI enforcement |
| Data plane | Supabase Postgres (+ optional per-org tenant projects) | Org-scoped rows / dedicated projects |
| API gate | FastAPI on Railway | Service-role DB access; no browser PostgREST mutations |
| Files | Supabase Storage | Paths prefixed with `auth_org_id` |
| Observability | Sentry, PostHog, Langfuse | Errors, product analytics, LLM traces |
| Billing | Stripe Billing | Checkout, Customer Portal, Smart Retries / dunning |

## Encryption

- **In transit:** All public HTTPS endpoints (Vercel, Railway, Supabase APIs). Enable
  “Enforce SSL” on Supabase database settings for direct Postgres clients. Prefer
  `sslmode=verify-full` for operator connections.
- **At rest:** Hosted Supabase encrypts disks by default. Application secrets stay in
  Railway/Vercel env vars, never in the browser.

## Logical vs physical isolation

- **Logical (default):** Every tenant-owned row carries `org_id` / `auth_org_id`.
  FastAPI scopes queries to the authenticated org. RLS helpers
  (`current_auth_org_id()`) defend Storage / future client access.
- **Physical (enterprise option):** `organization_tenants` + Management API provisioner
  can create a dedicated Supabase project per organization.

## Identity & access

- Roles: `org_admin` / `chief_compliance_officer` (Admin), `nominated_team_member`
  (Editor), `org_viewer` (Viewer), plus internal ops roles.
- MFA: Supabase TOTP is **required for all users** (enrollment + AAL2) unless
  `NEXT_PUBLIC_MFA_REQUIRED=false` / `MFA_REQUIRED_FOR_ALL_USERS=false` for local
  break-glass. Billing and support ticket mutations also require AAL2.
- SSO: Enable Supabase SAML on the Team/Enterprise plan for customer domains. Set
  `SSO_ENABLED=true` / `NEXT_PUBLIC_SSO_ENABLED=true` to surface the Settings
  placeholder and ops runbook (`docs/sso-saml-runbook.md`).

## Audit & monitoring

- Product audit trail: append-only `audit_logs` + UI at `/audit`.
- Extended events for org lifecycle, invites, billing, and marketing decisions where wired.
- System observability: Sentry (errors), PostHog (product), Langfuse (LLM). These fulfill
  the “Datadog / New Relic-class” launch requirement; Datadog can be added later if required.

## Usage analytics

- `usage_counters` + `GET /api/v1/usage/summary` expose org-level metric totals on Insights.

## GDPR / HIPAA / SOC 2 (shared responsibility)

| Control | Vendor / platform | Customer (IPX / Reg AI) |
| --- | --- | --- |
| SOC 2 Type 2 (infra) | Supabase / Vercel / Railway attestations | App security program, access reviews |
| GDPR | DPA with vendors; region selection | Lawful basis, notices, DSR / deletion workflows |
| HIPAA | Supabase BAA + High Compliance projects when needed | BAA execution, PHI minimization, policies |

Basic SOC 2 preparation pack:

- Control map: `docs/soc2-control-pack.md`
- Policies: `docs/policies/` (access control, incident response, vendor management, encryption)
- Vulnerability scanning: GitHub Actions `vulnerability-scan` + Dependabot in frontend/backend repos

Launch defaults:

1. Choose Supabase regions consistent with customer residency needs.
2. Execute vendor DPAs before processing personal data of EU residents.
3. Do **not** market HIPAA readiness until the BAA + High Compliance checklist is complete.
4. Maintain an org/user deletion runbook (auth user delete + cascading app data purge).
   - App API: `DELETE /api/v1/organizations/current` (org admin) purges shared-DB tenant rows.
   - Operator: delete the Supabase Auth user(s) in the Dashboard / Admin API; tear down any
     dedicated tenant Supabase project listed in `organization_tenants`.

## Subscription / dunning

Stripe is the system of record for payments. Webhooks update `subscriptions` and
entitlements. Smart Retries + Stripe emails handle dunning; the app surfaces
`past_due` status in Settings → Billing.

## Customer support

Crisp chat (optional `NEXT_PUBLIC_CRISP_WEBSITE_ID`) plus in-app support tickets
(`support_tickets`) with Resend notification when `SUPPORT_NOTIFY_EMAIL` is set.
