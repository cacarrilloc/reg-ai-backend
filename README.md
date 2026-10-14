# Reg AI Backend

Python FastAPI backend scaffold for the Reg-AI Policy Change Agent V1.

This service is intentionally built as a **modular monolith**: one deployable FastAPI app with clean internal modules for auth, organizations, policies, ingestion, regulatory monitoring, RAG, gap analysis, redline generation, review workflow, audit logging, notifications, and background jobs.

## Quick start

```bash
cd reg-ai-backend
pyenv install 3.12.11
pyenv global 3.12.11
pyenv local 3.12.11
rm -rf .venv

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp deploy/.railway.env.local .env
env -u OPENAI_API_KEY .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000/api/v1/health
http://localhost:8000/docs
```

## Database migration

Run the Phase 1 Supabase migration against the dev or test project:

```bash
cd reg-ai-backend
brew install supabase/tap/supabase
supabase login --token

## Link the project
supabase link --project-ref zkjritciuvmsinasnylg

## Create a migration called "migration-name"
supabase migration new <migration-name>

## Run all migrations for this project
supabase db push

```

Or run the Alembic wrapper against `DATABASE_URL`:

```
.venv/bin/alembic upgrade head
alembic upgrade head
```

The migration creates the 7 core tables, enables `pgvector`, provisions Storage buckets, and applies org-scoped RLS policies.

## Run tests

```bash
python -m pytest
python -m ruff check .
python -m mypy app tests
```

## Run Inngest Server

```
npx --yes inngest-cli@latest dev \
  --no-discovery \
  --no-poll \
  -u http://127.0.0.1:8000/api/v1/inngest
```

`--no-poll` stops the Dev Server from endlessly `PUT`ing `/api/v1/inngest`, which otherwise drowns out embedding/gap-analysis logs in the uvicorn terminal.
## LangFuse LLM Monitoring

```https://cloud.langfuse.com/```

## Project structure

```text
app/
  api/v1/endpoints/      HTTP route modules
  core/                  settings, logging, security helpers
  db/                    database session and base model setup
  models/                SQLAlchemy ORM models
  schemas/               Pydantic request/response schemas
  services/              business logic modules
  workers/               background job entrypoints
  utils/                 shared utility functions
```

## Environment variables

Copy `.env.backend` to `.env` and fill in the values as integrations are added.

For local development, the app runs without external services if `DATABASE_URL` is left unset. In that mode policy uploads are parsed (and embedded when an OpenAI key is configured), but are returned with `persisted: false`. Set `DATABASE_URL` only to a reachable PostgreSQL/Supabase project when persistence is required. Clerk auth bypass is enabled locally by default; set `AUTH_BYPASS_ENABLED=false` in Railway and other shared environments.

See `docs/phase-1-foundation.md` for Supabase, Clerk, and Inngest setup details.
See `docs/railway-deployment.md` for Railway + Vercel production deployment.
See `docs/observability-setup.md` for Sentry, Resend, Langfuse, and PostHog setup.
See `docs/v1-saas-plan-readiness.md` for the phase-by-phase compatibility matrix from the SaaS plan.

## Railway deployment (production backend)

Deploy the API on Railway and connect it to the Vercel frontend:

```text
Vercel (reg-ai-frontend) → Railway (reg-ai-backend) → Supabase
```

1. Connect this repo to Railway (uses `railway.json` + `Dockerfile`).
2. Copy variables from `deploy/railway.env.backend` into Railway → **Variables**.
3. Run migrations: `alembic upgrade head` (or `railway run alembic upgrade head`).
4. Set Vercel `NEXT_PUBLIC_API_BASE_URL` to the Railway public domain.
5. Verify:

```bash
chmod +x scripts/verify-railway-integration.sh
./scripts/verify-railway-integration.sh https://<your-railway-domain> https://reg-ai-frontend.vercel.app
```

Full walkthrough: [`docs/railway-deployment.md`](docs/railway-deployment.md).

## Phase 2 client data ingestion

Phase 2 adds a versioned policy library, org-scoped Supabase Storage uploads, approval-chain configuration, and optional onboarding context documents.

Before using the upload flows, set `SUPABASE_URL` to the project base URL (for example, `https://<project-ref>.supabase.co`) and provide a server-only `SUPABASE_SERVICE_ROLE_KEY`. Then apply the migration:

### Inngest Server:

Run Inngest server as follows:
```
npx --yes inngest-cli@latest dev \
  --no-discovery \
  -u http://127.0.0.1:8000/api/v1/inngest
```

```bash
alembic upgrade head
```

The API stores policy versions under `policy-documents/<org_id>/...` and onboarding context under `context-documents/<org_id>/...`. The first path segment intentionally matches the tenant identifier used by the Storage RLS policies.

Key endpoints:

- `POST /api/v1/policies/upload` — uploads and versions a policy; optional multipart field: `policy_name`.
- `GET /api/v1/policies` and `GET /api/v1/policies/{policy_id}/versions` — policy-library list and version history.
- `PUT /api/v1/organizations/current/approval-chain` — stores the organization structure and ordered approving authorities.
- `POST /api/v1/context-documents/{document_type}/upload` — uploads `PRIOR_EXAM_RESPONSE`, `REPORTING_HISTORY`, or `VIOLATION_HISTORY` context.

## Phase 3 regulatory monitoring

Phase 3 runs separate Inngest cron functions for SEC, FINRA, CFTC, and NFA. The cron expressions are UTC and configurable through `SEC_SCRAPE_CRON`, `FINRA_SCRAPE_CRON`, `CFTC_SCRAPE_CRON`, and `NFA_SCRAPE_CRON`.

- SEC reads official newsroom and RSS feeds via `SEC_NEWSROOM_URLS`; provide a real contact in `SEC_USER_AGENT`.
- FINRA, CFTC, and NFA use RSS URLs configured through `*_RSS_URLS`; FINRA entries are additionally fetched and parsed for their full text. Add the desired official NFA feeds to `NFA_RSS_URLS` from the NFA RSS catalogue.
- New source records are deduplicated by `(source, url)`, embedded, and synced into Regulations reference docs. Per-organization gap analysis runs via the `regulatory-compliance-workflow` cron (not a publication outbox).
- `GET /api/v1/regulatory/publications` returns the latest 50 records. Customer requests are limited to their configured regulator sources; internal roles see all sources. `POST /api/v1/regulatory/ingest` is limited to Master and Developer users for manual operational runs.

Apply `alembic upgrade head` or `supabase db push` before enabling the scheduled functions so the publication indexes exist.

## Phase 4 business-change triggers

`POST /api/v1/business-changes` records a dated business change, assesses whether policy review is required, and returns the assessment decision and rationale. With `ANTHROPIC_API_KEY` configured, the service calls `ANTHROPIC_OPUS_MODEL` (default: `claude-opus-4-6`) using a JSON-only assessment contract. Without an Anthropic key, the configured `BUSINESS_CHANGE_REVIEW_TYPES` taxonomy supplies a deterministic local decision; an unavailable or malformed LLM response fails safe and requires human review.

Flagged changes are committed before a `business_change.flagged` outbox row is created. Inngest delivers that event to `business-change-policy-change-agent`, which creates an org-specific policy-change agent request. Apply `alembic upgrade head` before enabling the flow so the assessment columns and event-delivery outbox exist.
