# Security Hardening (Test Phase + SOC 2 Prep)

This document covers the controls added for the Test Phase security checklist and how to operate them in staging/production.

## Controls implemented in code

| Control | Where | How to enable |
|---|---|---|
| Security headers | Backend `SecurityHeadersMiddleware`, Frontend `next.config.ts` `headers()` | On by default (`SECURITY_HEADERS_ENABLED=true`) |
| Inbound API rate limiting | Backend `InboundRateLimitMiddleware` | `RATE_LIMIT_ENABLED=true` (default), tune with `RATE_LIMIT_REQUESTS_PER_MINUTE` |
| Auth-failure throttling | Same middleware + Sentry events | `RATE_LIMIT_AUTH_FAILURES_PER_MINUTE` |
| IP allowlisting | Backend `IPAllowlistMiddleware` | Set `ALLOWED_IPS` and/or `ALLOWED_IP_CIDRS` (empty = allow all) |
| Failed auth monitoring | `report_failed_authentication()` + Clerk webhook | API 401s → structured logs + Sentry; Clerk lifecycle → `/api/v1/webhooks/clerk` |
| Clerk route protection | Frontend `app/(app)/layout.tsx` (`SignedInGate`) + bare `clerkMiddleware()` | Middleware no longer uses `createRouteMatcher` / path `auth.protect()` |
| Multi-role RBAC | Backend `app/core/roles.py` + endpoint guards | Already required for reviews/admin/org settings |
| Append-only audit trail | Postgres triggers + `/audit` UI | See `docs/phase_8_audit_hardening.md` |

## Basic SSO (Clerk)

Reg-AI uses **Clerk** as the established identity provider (IdP). Enterprise SSO is configured in Clerk Dashboard, not in application code.

### Recommended Clerk setup for internal testing

1. Clerk Dashboard → **User & Authentication → Social connections**
   - Enable Google and/or Microsoft for internal staff SSO testing.
2. Clerk Dashboard → **Enterprise SSO / SAML** (Clerk Pro/Enterprise)
   - Add your corporate IdP (Okta, Azure AD, Google Workspace).
   - Map organization membership and roles into Clerk Organizations.
3. Keep JWT template / session claims aligned with backend expectations:
   - Org claim: `org_id` (or `CLERK_ORG_CLAIM`)
   - Role claim: `metadata.role` / org role (`CLERK_ROLE_CLAIM`, plus Clerk `o.rol` / `org_role`)
4. Restrict sign-up in shared environments:
   - Disable public sign-up for staging/production.
   - Invite-only membership for customer orgs.

### Role model beyond Admin/User

Internal:

- Master User
- Developer User
- Product Manager
- Admin User

Customer:

- Chief Compliance Officer
- Nominated Team Member

Endpoint access is enforced with `has_any_role` / `require_internal_role`. Organization data is scoped by Clerk `org_id`.

## IP restrictions (test / staging)

Use allowlisting on the **staging API** so only office / VPN / CI IPs can reach it:

```bash
ENVIRONMENT=staging
ALLOWED_IPS=203.0.113.10,198.51.100.20
ALLOWED_IP_CIDRS=10.0.0.0/8,192.168.0.0/16
```

Notes:

- Health probes (`/health`, `/api/v1/health`) remain reachable for Railway/Vercel checks.
- Exact IPs and CIDRs can be mixed.
- Leave both empty for local development and unrestricted production unless you intentionally lock production behind a VPN/WAF.

Platform complement (recommended):

- Vercel Firewall / Deployment Protection for the staging frontend
- Railway private networking or edge firewall rules for the staging API

## Security headers

Backend responses include:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy`
- `Content-Security-Policy` (API: `default-src 'none'`; skipped on `/docs` and `/redoc`)
- `Strict-Transport-Security` in staging/production

Do **not** set `Cross-Origin-Resource-Policy` on API responses — `same-site`/`same-origin` makes browsers fail cross-origin `fetch` from the Next.js frontend (surfaces as a NetworkError). Do **not** set `Cross-Origin-Opener-Policy` on the Next.js app either — COOP breaks Clerk session refresh / OAuth.

Frontend Next.js adds browser-facing headers via `headers()` in `next.config.ts` (nosniff, frame deny, referrer-policy, permissions-policy, HSTS on staging/production).

## Rate limiting

Default inbound limit: **120 requests / IP / minute**.

Auth failures (HTTP 401/403) are tracked separately; repeated failures log `auth_failure_rate_limit_exceeded`.

Tune:

```bash
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS_PER_MINUTE=120
RATE_LIMIT_AUTH_FAILURES_PER_MINUTE=30
```

This is an application-layer control. For production DDoS, also enable platform WAF / Cloudflare / Vercel Attack Challenge.

## Failed login / auth monitoring

Two layers:

1. **API authentication failures** (invalid/missing Clerk JWT)
   - Structured log event: `authentication_failed`
   - Sentry warning with fingerprint `authentication_failed`
2. **Clerk webhook lifecycle events**
   - Endpoint: `POST /api/v1/webhooks/clerk`
   - Secret: `CLERK_WEBHOOK_SECRET` (Svix `whsec_...`)
   - Subscribe at least to: `session.created`, `session.ended`, `session.removed`, `session.revoked`
   - Revoked/ended/removed sessions emit security warnings into logs/Sentry

Also enable Clerk Dashboard → **Attack protection** (bot protection, disposable email blocking, password breach checks) for credential-stuffing coverage that happens before a JWT ever reaches the API.

## Staging environment checklist

See `docs/staging-environment.md` for the full standing staging setup.

Minimum:

- Separate Vercel project or protected preview alias (e.g. `staging.reg-ai...`)
- Separate Railway service with `ENVIRONMENT=staging`
- Separate Supabase project **or** clearly isolated staging schema/DB
- Separate Clerk application (test instance)
- `AUTH_BYPASS_ENABLED=false`
- IP allowlist enabled for the staging API
- Sentry environment tag = `staging`

## SOC 2 evidence map

| Control area | In-product evidence | Operator evidence still needed |
|---|---|---|
| Access control / MFA | Forced TOTP enrollment + AAL2 gate; RBAC; RLS | Quarterly access reviews of Supabase/Vercel/Railway/GitHub |
| Audit logging | Append-only `audit_logs`, Audit Trail UI | Retention period + restore test proof |
| Change management | GitHub PRs / CI | Written change-management policy acknowledgment |
| Monitoring | Sentry, PostHog, Langfuse, auth-failure events | Alert routing on-call runbook |
| Network controls | App IP allowlist + headers + rate limits | WAF / firewall screenshots for staging |
| Encryption | HTTPS (Vercel/Railway), Supabase TLS, `/health` TLS flags | Key management / backup encryption attestation |
| Vulnerability mgmt | Weekly + PR `npm audit` / `pip-audit`, Dependabot | Triage SLA for high/critical findings. Accepted exception: `PYSEC-2026-3740` (nltk 3.10.3, no upstream patch; unused model-artifact APIs). |
| Policies | `docs/policies/*`, `docs/soc2-control-pack.md` | Annual policy sign-off |

See `docs/soc2-control-pack.md` for the basic four-requirement checklist.

## Environment variables (security)

Backend:

```bash
ENVIRONMENT=staging
SECURITY_HEADERS_ENABLED=true
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS_PER_MINUTE=120
RATE_LIMIT_AUTH_FAILURES_PER_MINUTE=30
ALLOWED_IPS=
ALLOWED_IP_CIDRS=
CLERK_WEBHOOK_SECRET=
AUTH_BYPASS_ENABLED=false
```

Frontend:

```bash
NEXT_PUBLIC_APP_ENV=staging
# Clerk publishable + secret keys must be from the same Dashboard instance.
# Local clerkMiddleware is off by default; set CLERK_MIDDLEWARE_ENABLED=true to test.
# App pages are gated by app/(app)/SignedInGate, not middleware path matchers.
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_test_...
```
