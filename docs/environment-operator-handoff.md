# Environment operator handoff

Use this checklist after cloud projects exist (`reg-ai-staging` / `reg-ai-production` on Vercel, Railway `staging`/`production`, Supabase Staging/Prod Reg AI).

Full runbook: [`staging-environment.md`](staging-environment.md).

## 1. Paste these values (you gather from dashboards)

### Supabase → Staging Reg AI (us-west-2)

| Field | Where | Paste into |
|---|---|---|
| Project URL `https://<ref>.supabase.co` | Settings → API | Railway staging `SUPABASE_URL`; `.env.staging` |
| `service_role` secret (**JWT** `eyJ…`) | Settings → API | Railway staging `SUPABASE_SERVICE_ROLE_KEY` |
| DB password + session pooler URI `:5432` | Settings → Database | Railway staging + `.env.staging` `DATABASE_URL` |

### Supabase → Prod Reg AI (us-east-2)

| Field | Where | Paste into |
|---|---|---|
| Project URL | Settings → API | Railway production `SUPABASE_URL`; `.env.prod` |
| `service_role` JWT | Settings → API | Railway production `SUPABASE_SERVICE_ROLE_KEY` |
| DB password + session pooler URI `:5432` | Settings → Database | Railway production + `.env.prod` `DATABASE_URL` |

Project ref already referenced in local `.env.prod` for Prod: `rednxostzguusaagguzo` — confirm in dashboard and replace password / service_role placeholders.

### Railway → reg-ai-backend

| Field | Where | Paste into |
|---|---|---|
| Staging public URL | staging env → Settings → Networking | Vercel `reg-ai-staging` → `NEXT_PUBLIC_API_BASE_URL` |
| Production public URL | production env → Settings → Networking | Vercel `reg-ai-production` → `NEXT_PUBLIC_API_BASE_URL` |

### Clerk

| Field | Staging | Production |
|---|---|---|
| Publishable key | Vercel staging + note for docs | Vercel production |
| Secret key | Vercel + Railway that env | Vercel + Railway that env |
| Issuer + JWKS | Railway `CLERK_ISSUER` / `CLERK_JWKS_URL` | same |
| Webhook signing secret | Railway `CLERK_WEBHOOK_SECRET` | Railway `CLERK_WEBHOOK_SECRET` |

Issuer host must return HTTP 200 for `/.well-known/jwks.json` before deploy.

### Inngest

| Field | Staging | Production |
|---|---|---|
| Event key | Railway staging | Railway production |
| Signing key | Railway staging | Railway production |
| App id | `reg-ai-backend-staging` | `reg-ai-backend-production` |
| Sync URL | `https://<staging-api>/api/v1/inngest` | `https://<production-api>/api/v1/inngest` |

## 2. Apply config

1. Railway staging Raw Editor ← `deploy/railway.env.staging.example` + secrets.
2. Railway production Raw Editor ← `deploy/railway.env.production.example` + secrets.
3. Vercel `reg-ai-staging` ← `deploy/vercel.env.staging.example` + Railway staging URL.
4. Vercel `reg-ai-production` ← `deploy/vercel.env.production.example` + Railway production URL.
5. Redeploy both Vercel projects.
6. Run migrations:

```bash
cd reg-ai-backend
./scripts/migrate-environment.sh staging
./scripts/migrate-environment.sh production
```

## 3. Verify health (run after URLs are live)

```bash
# Staging
curl -sS "$STAGING_API/api/v1/health" | python3 -m json.tool
# Expect: environment=staging, auth_bypass_enabled=false, deployment_ready=true

# Production
curl -sS "$PRODUCTION_API/api/v1/health" | python3 -m json.tool
# Expect: environment=production, auth_bypass_enabled=false, deployment_ready=true

# CORS preflight (staging example)
curl -i -X OPTIONS "$STAGING_API/api/v1/policies" \
  -H "Origin: https://reg-ai-staging.vercel.app" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization,content-type"
```

Or:

```bash
./scripts/verify-railway-integration.sh "$STAGING_API" "https://reg-ai-staging.vercel.app"
./scripts/verify-railway-integration.sh "$PRODUCTION_API" "https://reg-ai-production.vercel.app"
```

## 4. Browser smoke

1. `https://reg-ai-staging.vercel.app` — Clerk loads, sign-in works, dashboard hits staging API.
2. `https://reg-ai-production.vercel.app` — same for production.
3. Confirm staging uploads do not appear in Prod Reg AI.

## 5. Verification snapshot (2026-08-01)

Public hosts discovered and probed:

| URL | Result |
|---|---|
| `https://reg-ai-backend-production.up.railway.app/api/v1/health` | HTTP 200 — `environment=production`, but `database_reachable=false` (`ProgrammingError`) |
| `https://reg-ai-backend-dev.up.railway.app/api/v1/health` | HTTP 200 — still reports `environment=production` (staging env vars not applied yet) + same DB error |
| `https://reg-ai-staging.vercel.app` | HTTP 500 `MIDDLEWARE_INVOCATION_FAILED` (almost always bad/missing `CLERK_SECRET_KEY`) |
| `https://reg-ai-production.vercel.app` | HTTP 200 HTML, but still embeds dead `pk_live_…` for `clerk.reg-ai.ipxenterprises.com` |

**Immediate dashboard fixes (in order):**

1. Railway **staging** Raw Editor: set `ENVIRONMENT=staging` and Staging Reg AI `DATABASE_URL` (session `:5432`, no `?pgbouncer=true`).
2. Railway **production**: fix Prod Reg AI `DATABASE_URL` the same way (ProgrammingError is often the Prisma `pgbouncer` query flag or wrong password).
3. Vercel **reg-ai-staging**: set valid `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` + matching `CLERK_SECRET_KEY`; `NEXT_PUBLIC_API_BASE_URL=https://reg-ai-backend-dev.up.railway.app`; redeploy.
4. Vercel **reg-ai-production**: replace dead `pk_live_` with a reachable Clerk instance; `NEXT_PUBLIC_API_BASE_URL=https://reg-ai-backend-production.up.railway.app`; redeploy.
5. Run `./scripts/migrate-environment.sh staging` and `… production` after DATABASE_URL works.
