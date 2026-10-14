# Staging + Production Environments

Goal: two standing environments with isolated data, identity, and APIs — safe for demos / SOC 2 prep (staging) and customer traffic (production).

Both frontends and both Railway environments deploy from **`main`**, differentiated only by project/environment variables.

## Topology (current cloud projects)

| Layer | Staging | Production |
|---|---|---|
| Frontend (Vercel) | `reg-ai-staging` → `https://reg-ai-staging.vercel.app` | `reg-ai-production` → `https://reg-ai-production.vercel.app` |
| Backend (Railway) | Project `reg-ai-backend` → environment **staging** | Project `reg-ai-backend` → environment **production** |
| Database / storage (Supabase) | **Staging Reg AI** (AWS us-west-2) | **Prod Reg AI** (AWS us-east-2) |
| Identity (Clerk) | Dedicated test app (`pk_test_` / `sk_test_`) | Dedicated app with a **reachable** Frontend API |
| Jobs (Inngest) | Separate app or keys (`INNGEST_APP_ID=reg-ai-backend-staging`) | Production keys (`INNGEST_APP_ID=reg-ai-backend-production`) |

```text
Browser → Vercel (reg-ai-staging | reg-ai-production)
              ↓  HTTPS + Clerk JWT
         Railway (staging | production)
              ↓
         Supabase (Staging Reg AI | Prod Reg AI)
```

Do **not** point staging at production Supabase service-role keys or production Clerk secrets.

## Variable matrix

### Frontend (Vercel project env vars)

| Variable | Staging (`reg-ai-staging`) | Production (`reg-ai-production`) |
|---|---|---|
| `NEXT_PUBLIC_APP_ENV` | `staging` | `production` |
| `NEXT_PUBLIC_API_BASE_URL` | Railway **staging** public URL | Railway **production** public URL |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Staging Clerk `pk_test_…` | Prod Clerk publishable key |
| `CLERK_SECRET_KEY` | Staging Clerk `sk_test_…` | Prod Clerk secret key |
| `NEXT_PUBLIC_CLERK_JWT_TEMPLATE` | `reg-ai-api` (if used) | same template name in that Clerk app |

Templates: [`reg-ai-frontend/deploy/vercel.env.staging.example`](../../reg-ai-frontend/deploy/vercel.env.staging.example), [`vercel.env.production.example`](../../reg-ai-frontend/deploy/vercel.env.production.example).

`NEXT_PUBLIC_API_BASE_URL` must be the **Railway API origin**, never Clerk and never the Vercel frontend URL.

### Backend (Railway environment variables)

| Variable | Staging | Production |
|---|---|---|
| `ENVIRONMENT` | `staging` | `production` |
| `AUTH_BYPASS_ENABLED` | `false` | `false` |
| `CORS_ORIGINS` / `FRONTEND_URL` / `CLERK_AUTHORIZED_PARTIES` | `https://reg-ai-staging.vercel.app` | `https://reg-ai-production.vercel.app` |
| `DATABASE_URL` | Staging Reg AI session pooler `:5432` | Prod Reg AI session pooler `:5432` |
| `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` | Staging project | Prod project |
| `CLERK_ISSUER` + `CLERK_JWKS_URL` + `CLERK_SECRET_KEY` | Staging Clerk | Prod Clerk |
| `INNGEST_APP_ID` | `reg-ai-backend-staging` | `reg-ai-backend-production` |

Templates: [`deploy/railway.env.staging.example`](../deploy/railway.env.staging.example), [`deploy/railway.env.production.example`](../deploy/railway.env.production.example).

**URL rules**

- No quotes in Railway Raw Editor.
- No trailing slash on origins.
- `DATABASE_URL`: `postgresql+psycopg://…@….pooler.supabase.com:5432/postgres` — **no** `?pgbouncer=true`.
- `SUPABASE_SERVICE_ROLE_KEY`: legacy `eyJ…` JWT for that project (Storage rejects `sb_secret_` keys).

## Railway dual-environment setup

1. Open Railway project **reg-ai-backend**.
2. Environment dropdown → **staging**.
3. If no service exists: **duplicate** the production `reg-ai-backend` service into staging (same GitHub repo / `railway.json`).
4. **Settings → Networking → Generate Domain** for staging (note the `*.up.railway.app` URL).
5. **Variables → Raw Editor**: paste from `deploy/railway.env.staging.example` and fill secrets.
6. Switch to **production** → paste from `deploy/railway.env.production.example` with prod secrets.
7. Confirm both services are **Online** and healthchecks pass (`GET /api/v1/health`).

## Vercel dual-project setup

1. Project **reg-ai-staging** → Environment Variables → set from `vercel.env.staging.example` (Production scope for that project).
2. Set `NEXT_PUBLIC_API_BASE_URL` to the Railway **staging** domain from the previous section.
3. Project **reg-ai-production** → same with production values and Railway **production** API URL.
4. Redeploy both projects (required after any `NEXT_PUBLIC_*` change).
5. Optional: enable **Deployment Protection** on staging.

## Clerk checklist

### Staging

1. Dedicated Clerk application (Development / test keys).
2. Disable public sign-up; invite-only.
3. Allowed origins / redirect URLs: `https://reg-ai-staging.vercel.app`.
4. Webhook: `https://<staging-railway>/api/v1/webhooks/clerk` → `CLERK_WEBHOOK_SECRET` on Railway staging.
5. JWT template `reg-ai-api` with `org_id` + role claims if the app uses it.

### Production

1. Dedicated Clerk application.
2. Frontend API host must **resolve** (use `*.clerk.accounts.dev` until custom DNS is verified). Do **not** use `clerk.reg-ai.ipxenterprises.com` until DNS + cert are live.
3. Allowed origins: `https://reg-ai-production.vercel.app`.
4. Webhook: `https://<production-railway>/api/v1/webhooks/clerk`.
5. `AUTH_BYPASS_ENABLED=false` on Railway production.

## Database migrations

See [Environment migrations](#environment-migrations) below (also covered in `railway-deployment.md`).

```bash
cd reg-ai-backend
./scripts/migrate-environment.sh staging
./scripts/migrate-environment.sh production
```

Fill `DATABASE_URL` in `.env.staging` / `.env.prod` first (no placeholders).

## Smoke tests

1. `curl -s https://<staging-api>/api/v1/health | python3 -m json.tool`  
   Expect `environment=staging`, `auth_bypass_enabled=false`, `deployment_ready=true` (when fully configured).
2. Same for production API → `environment=production`.
3. Open `https://reg-ai-staging.vercel.app` → Clerk sign-in works → dashboard loads data from staging only.
4. Open `https://reg-ai-production.vercel.app` → Clerk sign-in works.
5. Run Upload / Sync now on **staging** first; confirm Prod Reg AI row counts unchanged.
6. Deliberate bad bearer token → `401` / auth failure logged (optional Sentry).

## Operator paste list (fill before go-live)

Copy from each dashboard into Railway / Vercel / local `.env.*`:

| Source | Values needed |
|---|---|
| Supabase Staging Reg AI | Project URL, `service_role` JWT, DB password, session pooler host |
| Supabase Prod Reg AI | Project URL, `service_role` JWT, DB password, session pooler host |
| Railway staging | Public API hostname |
| Railway production | Public API hostname |
| Clerk staging | `pk_test_`, `sk_test_`, issuer slug, JWKS URL, webhook secret |
| Clerk production | Publishable + secret keys, issuer, JWKS, webhook secret |
| Inngest | Staging event + signing keys; production event + signing keys |

## SOC 2 operator evidence

- Screenshot/export of staging vs production env var **names** (no secret values in tickets).
- Access list for who can deploy staging and production.
- Clerk staging membership export.
- Date of last staging `alembic upgrade head` / restore dry-run.

## Environment migrations

### URL form (required)

```text
postgresql+psycopg://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:5432/postgres
```

| Do | Don't |
|---|---|
| Port **5432** (session pooler) for Alembic + app | Port 6543 unless you accept disabled prepared statements |
| `postgresql+psycopg://` | Bare `postgresql://` if you need psycopg3 `prepare_threshold` |
| Omit query string | `?pgbouncer=true` (Prisma-only; SQLAlchemy fails) |

### Steps

1. Supabase → project → **Project Settings → Database** → copy URI (session mode) and password.
2. Put the URL in `.env.staging` or `.env.prod` as `DATABASE_URL`.
3. From `reg-ai-backend`:

```bash
./scripts/migrate-environment.sh staging     # Staging Reg AI
./scripts/migrate-environment.sh production  # Prod Reg AI
```

4. Confirm with a simple query or by checking Alembic version table in the Supabase SQL editor.

Or via Railway CLI (linked to the right environment):

```bash
railway link   # select staging or production
railway run alembic upgrade head
```
