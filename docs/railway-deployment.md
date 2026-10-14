# Railway deployment (reg-ai-backend → Vercel frontends)

This guide deploys the FastAPI backend on **Railway** (staging + production environments) and connects each to the matching **Vercel** frontend and **Supabase** project.

For the full dual-environment matrix, Clerk/Inngest checklists, and smoke tests, see [`staging-environment.md`](staging-environment.md).

## Architecture

```text
Browser → Vercel (reg-ai-staging | reg-ai-production)
              ↓  HTTPS + Clerk JWT
         Railway (staging | production)
              ↓
         Supabase (Staging Reg AI | Prod Reg AI)
```

The frontend never talks to Supabase directly. All data flows through the Railway API.

Both Railway environments and both Vercel projects track **`main`**; secrets differ per environment/project.

## 1. Railway project and environments

1. Open [railway.app](https://railway.app) project **reg-ai-backend**.
2. Confirm environments exist: **production** and **staging** (Environment dropdown).
3. In each environment, ensure a `reg-ai-backend` service is linked to the GitHub repo.
   - If staging is empty: duplicate the production service into the staging environment.
4. Railway uses [`railway.json`](../railway.json) (Dockerfile build, healthcheck `/api/v1/health`).
5. **Settings → Networking → Generate Domain** in **each** environment.
   - Example production: `https://reg-ai-backend-production.up.railway.app`
   - Example staging: `https://reg-ai-backend-dev.up.railway.app`

## 2. Configure Railway variables

**Service → Variables → Raw Editor** (per environment)

| Environment | Template |
|-------------|----------|
| staging | [`deploy/railway.env.staging.example`](../deploy/railway.env.staging.example) |
| production | [`deploy/railway.env.production.example`](../deploy/railway.env.production.example) |

Legacy single-file template: [`deploy/railway.env.example`](../deploy/railway.env.example).

### Minimum required (each environment)

| Variable | Staging example | Production example |
|----------|-----------------|--------------------|
| `ENVIRONMENT` | `staging` | `production` |
| `AUTH_BYPASS_ENABLED` | `false` | `false` |
| `CORS_ORIGINS` | `https://reg-ai-staging.vercel.app` | `https://reg-ai-production.vercel.app` |
| `FRONTEND_URL` | same as CORS | same as CORS |
| `CLERK_AUTHORIZED_PARTIES` | same as CORS | same as CORS |
| `DATABASE_URL` | Staging Reg AI session pooler **:5432** | Prod Reg AI session pooler **:5432** |
| `SUPABASE_URL` | Staging project URL | Prod project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Staging `eyJ…` JWT | Prod `eyJ…` JWT |
| `CLERK_ISSUER` | `https://<slug>.clerk.accounts.dev` | reachable issuer |
| `CLERK_JWKS_URL` | `…/.well-known/jwks.json` | matching JWKS |
| `CLERK_SECRET_KEY` | staging secret | prod secret |

### Notes

- **No quotes** in Railway’s raw editor (unlike local `.env` files).
- **No trailing slash** on URLs.
- `CORS_ORIGINS`, `FRONTEND_URL`, and `CLERK_AUTHORIZED_PARTIES` should match the Vercel origin. The backend merges them automatically.
- Use session pooler port **5432**. Do **not** append `?pgbouncer=true`.
- Prefer `postgresql+psycopg://` for the app and Alembic.
- Keep `DATABASE_POOL_SIZE` modest (e.g. 8) on Supabase Nano/small plans.
- Prefer separate Inngest keys and `INNGEST_APP_ID` per environment.

Save variables — Railway redeploys automatically.

## 3. Run database migrations

Apply Alembic to **each** Supabase project (staging and production are separate databases).

### Recommended script

```bash
cd reg-ai-backend
# Fill DATABASE_URL in .env.staging / .env.prod first (no REPLACE_ placeholders)
./scripts/migrate-environment.sh staging
./scripts/migrate-environment.sh production
```

### Manual

```bash
export DATABASE_URL="postgresql+psycopg://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres"
.venv/bin/alembic upgrade head
```

Or with Railway CLI (linked to the target environment):

```bash
railway link
railway run alembic upgrade head
```

### Connection string rules

| Correct | Incorrect |
|---------|-----------|
| Port `5432` | Relying on `6543` without disabling prepared statements |
| No `?pgbouncer=true` | Prisma-style `?pgbouncer=true` (SQLAlchemy/psycopg reject it) |
| `postgresql+psycopg://` | Wrong project’s password or service_role JWT |

## 4. Connect Vercel

### Staging — project `reg-ai-staging`

| Variable | Value |
|----------|-------|
| `NEXT_PUBLIC_APP_ENV` | `staging` |
| `NEXT_PUBLIC_API_BASE_URL` | Railway **staging** public URL |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Staging Clerk publishable key |
| `CLERK_SECRET_KEY` | Staging Clerk secret key |

Template: [`reg-ai-frontend/deploy/vercel.env.staging.example`](../../reg-ai-frontend/deploy/vercel.env.staging.example).

### Production — project `reg-ai-production`

| Variable | Value |
|----------|-------|
| `NEXT_PUBLIC_APP_ENV` | `production` |
| `NEXT_PUBLIC_API_BASE_URL` | Railway **production** public URL |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | Prod Clerk publishable key |
| `CLERK_SECRET_KEY` | Prod Clerk secret key |

Template: [`reg-ai-frontend/deploy/vercel.env.production.example`](../../reg-ai-frontend/deploy/vercel.env.production.example).

Redeploy Vercel after changing any `NEXT_PUBLIC_*` variable.

Historical project `reg-ai-frontend` / `reg-ai-frontend-blush.vercel.app` is superseded by `reg-ai-production` / `reg-ai-staging`; leave or delete separately.

## 5. Clerk dashboard

1. **Staging app** — origins `https://reg-ai-staging.vercel.app`; webhook → `https://<staging-api>/api/v1/webhooks/clerk`.
2. **Production app** — origins `https://reg-ai-production.vercel.app`; webhook → production API.
3. Frontend API host must resolve (use `*.clerk.accounts.dev` until custom DNS is verified).
4. Optional JWT template `reg-ai-api` with `org_id` and role claims.
5. Users must sign in with an **active organization** selected.

## 6. Verify deployment

```bash
chmod +x scripts/verify-railway-integration.sh

./scripts/verify-railway-integration.sh \
  https://<staging-or-production-api>.up.railway.app \
  https://reg-ai-staging.vercel.app   # or reg-ai-production.vercel.app
```

Or manually:

```bash
curl -s https://<railway-domain>/api/v1/health | python3 -m json.tool

curl -i -X OPTIONS "https://<railway-domain>/api/v1/reviews/audit-chains" \
  -H "Origin: https://reg-ai-staging.vercel.app" \
  -H "Access-Control-Request-Method: GET" \
  -H "Access-Control-Request-Headers: authorization,content-type,x-org-id"
```

### Health response fields

| Field | Staging | Production |
|-------|---------|------------|
| `environment` | `staging` | `production` |
| `cors_frontend_configured` | `true` | `true` |
| `database_configured` | `true` | `true` |
| `database_reachable` | `true` | `true` |
| `clerk_configured` | `true` | `true` |
| `supabase_configured` | `true` | `true` |
| `auth_bypass_enabled` | `false` | `false` |
| `deployment_ready` | `true` | `true` |

## 7. Railway configuration reference

[`railway.json`](../railway.json):

- **Builder:** Dockerfile
- **Health check:** `GET /api/v1/health`
- **Start command:** `uvicorn` on Railway’s `PORT`

[`Dockerfile`](../Dockerfile):

- Python 3.12 slim image
- Respects `${PORT:-8000}` at runtime

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Browser `NetworkError` / CORS | Set `CORS_ORIGINS` + `FRONTEND_URL` on Railway; redeploy |
| `401 Unauthorized token presenter` | Set `CLERK_AUTHORIZED_PARTIES` to the matching Vercel URL |
| `503 ... configured database` | Set `DATABASE_URL` on Railway |
| Clerk UI never loads | Publishable key’s Frontend API host must resolve (not a dead custom domain) |
| Empty UI, API returns `[]` | Clerk org ID must match rows in that env’s Supabase `organizations` |
| `environment: local` in health | Set `ENVIRONMENT=staging` or `production` on Railway |
| `invalid connection option "pgbouncer"` | Remove `?pgbouncer=true` from `DATABASE_URL` |
| `Invalid Compact JWS` on storage | Use legacy `service_role` JWT, not `sb_secret_` |
| Staging writes appear in prod | Wrong Supabase keys on that Railway environment |

Check Railway **Deploy Logs** after saving variables for structured startup hints.

## Optional: Inngest, Sentry, Langfuse

Set `INNGEST_EVENT_KEY`, `INNGEST_SIGNING_KEY`, `SENTRY_DSN`, and Langfuse keys on **each** Railway environment. Prefer separate Inngest apps so staging jobs cannot enqueue into production.
