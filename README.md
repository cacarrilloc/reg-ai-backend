# Reg AI Backend

Python FastAPI backend scaffold for the Reg-AI Policy Change Agent V1.

This service is intentionally built as a **modular monolith**: one deployable FastAPI app with clean internal modules for auth, organizations, policies, ingestion, regulatory monitoring, RAG, gap analysis, redline generation, review workflow, audit logging, notifications, and background jobs.

<img width="812" height="595" alt="reg-ai-diagram" src="https://github.com/user-attachments/assets/6fbbf743-573b-4ff7-af24-118f7220b188" />

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

# Backend Architecture Review

Access the repository through the connected GitHub integration and reviewed their directory structures, dependency declarations, authentication code, API communication, database tenancy, regulatory workflows, AI integration, review logic, and testing organization.

[reg-ai-backend](https://github.com/cacarrilloc/reg-ai-backend?utm_source=chatgpt.com)

main

Overall assessment: Reg-AI has evolved beyond a basic compliance chatbot into a substantial, multi-tenant regulatory compliance platform. Its architecture combines regulatory ingestion, retrieval-augmented analysis, AI-generated policy revisions, human approval workflows, marketing compliance review, and auditability.

This is a source-code architecture review, not a completed runtime or penetration test. I inspected representative critical implementations and the repository inventories, but did not execute the full test suites or verify production configuration.

## 1. System architecture

Logical architecture derived from the current repository implementations. External services are shown by responsibility rather than physical network topology.

### Technology stack

| Layer                 | Current implementation                     |
| --------------------- | ------------------------------------------ |
| Frontend              | Next.js 16, React 19, TypeScript           |
| UI                    | Tailwind CSS, Radix UI, Recharts           |
| Authentication        | Supabase Auth with JWT                     |
| Backend               | Python, FastAPI, Pydantic                  |
| Persistence           | PostgreSQL, SQLAlchemy, Alembic            |
| Vector retrieval      | pgvector and LlamaIndex-related components |
| AI                    | LiteLLM abstraction, configurable models   |
| Background processing | Inngest and FastAPI background tasks       |
| Storage               | Supabase Storage                           |
| Monitoring            | Sentry, Langfuse, PostHog                  |
| Deployment            | Vercel frontend, Railway backend           |

## 2. Frontend analysis

The frontend is organized into authenticated workspaces with reusable components for each compliance domain.

The most important parts are:

| Area            | Implementation            | Responsibility                                  |
| --------------- | ------------------------- | ----------------------------------------------- |
| Dashboard       | `app/(app)/dashboard`     | Compliance overview and approvals               |
| Policy library  | `components/policies`     | Uploads, policy inventory, versions             |
| Compliance gaps | `components/gap-analyses` | Findings, regulatory citations                  |
| Review queue    | `components/reviews`      | AI revisions and human decisions                |
| Marketing       | `components/marketing`    | Marketing content review and deliverables       |
| Authentication  | `components/auth`         | Session, MFA, organization access               |
| Shared API      | `lib/api.ts`              | Authorization headers, HTTP requests, downloads |

The frontend has several positive design characteristics: a centralized API client, reusable authentication hooks, organization-aware routing, and dedicated tests for utility logic.

One important architectural detail is that `lib/api.ts` supports both a same-origin `/backend-api` proxy and direct backend requests. Multipart uploads use the direct path to avoid proxy body limits, while certain long-running calls can bypass the proxy explicitly.

Potential concern: The frontend contains several very large components, including the review queue workspace and presentation deliverable component. These should be candidates for decomposition into domain hooks, state management, and smaller view components.

Relevant implementation: [Frontend API client](https://github.com/cacarrilloc/reg-ai-frontend/blob/main/lib/api.ts?utm_source=chatgpt.com).

## 3. Backend analysis

The backend is a modular monolith. That is a reasonable architectural choice for Reg-AI's current product scope: it keeps business transactions and compliance workflows coordinated without prematurely distributing everything across independent services.

Its main layers are:

- `app/api/v1/endpoints/` — HTTP contracts and request handling.
- `app/services/` — compliance workflows and domain logic.
- `app/ai/` — model configuration, credentials, adapters, and metering.
- `app/models/` and `app/schemas/` — persistence and validation.
- `app/db/` — tenant-aware database sessions.
- `app/workers/` — background job entry points.

### Core compliance workflow

Regulatory / Business Trigger

SEC · FINRA · CFTC · NFA / Internal changes

Document Retrieval

Stored policies · embeddings · pgvector

Gap Analysis

Evidence · findings · impact scoring

Redline Generation

Constrained policy revision + validation

Human Compliance Review

Assignment · approval · rejection

Audit and Compliance History

Decision records · version tracking

The most consequential services include:

`gap_analysis_service.py` — handles trigger context, policy selection, retrieval, gap identification, fallback behavior, and persistence.

`redline_generation_service.py` — manages structured revision generation, validation, document output, and similarity checks.

`review_service.py` — manages assignments, decisions, audit information, and notifications.

`marketing_review_engine.py` — combines classification, deterministic compliance checks, and model-assisted review.

`regulatory_compliance_workflow_service.py` — coordinates regulatory synchronization and policy reanalysis.

This separation is conceptually strong. However, several individual service files exceed 1,700–3,000 lines, suggesting increasing maintenance complexity and a need for narrower responsibilities.

## 4. AI architecture

One of the stronger design choices is the provider abstraction in `app/ai/factory.py`.

Rather than having each workflow call OpenAI or Anthropic directly, the code exposes provider interfaces and resolves the appropriate implementation for an organization and purpose.

That supports model configuration for analysis, embeddings, and redline generation without tightly coupling every workflow to one provider.

The gap analysis implementation also contains explicit fallback handling, while redline generation includes structured models and validation logic. These are important foundations for an AI system used in regulated environments.

The central quality challenge is evidence fidelity: every material AI finding should be traceable to the exact source document, regulation, version, and relevant passage. Similarity scores alone do not establish regulatory correctness.

I would prioritize evaluating citation accuracy, unsupported assertions, cross-document contamination, and the reproducibility of previous compliance decisions.

## 5. Security and multi-tenancy

The backend has an extensive security implementation, including JWT verification, role and permission checks, MFA-related controls, tenant-aware database sessions, and security middleware.

The database layer supports organization-specific schema routing and shared platform data. It uses transaction-local PostgreSQL `search_path` configuration, which is relevant when operating behind a connection pooler.

There are nevertheless several areas requiring focused verification.

| Priority | Finding                                                                          | Recommended action                                                             |
| -------- | -------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| High     | Non-production auth bypass and header overrides are implemented                  | Ensure no shared deployment can accept caller-controlled identity headers      |
| High     | Tenant isolation spans JWT claims, membership lookup, DB routing, and storage    | Run adversarial cross-tenant authorization tests across every resource family  |
| Medium   | Some workflows use process-local asynchronous tasks                              | Check recovery, retries, deduplication, and cancellation during restarts       |
| Medium   | Database and application authorization interact across multiple layers           | Verify RBAC, RLS, schema isolation, and membership-cache invalidation together |
| Medium   | Extensive document ingestion and AI processing increase untrusted-input exposure | Test prompt injection, malicious file content, and output sanitization         |

These are review priorities, not confirmed exploitable vulnerabilities.

A concrete example is `get_current_user()` in [backend security.py](https://github.com/cacarrilloc/reg-ai-backend/blob/main/app/core/security.py?utm_source=chatgpt.com). Local header overrides are conditional on configuration and the application not being in production mode. I would verify that these controls are fail-closed for staging, preview, and any internet-accessible testing deployment.

## 6. Engineering maturity and technical debt

Good foundations

Modular domain services, typed API contracts, a substantial backend test inventory, frontend unit tests, explicit authorization helpers, and observability integrations.

Documentation drift

The frontend README describes Next.js 15 and Clerk, whereas the current package configuration specifies Next.js 16 and the authentication implementation uses Supabase. Backend documentation also contains legacy Clerk references.

Codebase complexity

Large workflow services and UI components can make isolated changes, regression testing, and code review more difficult.

Repository hygiene

The backend tracks Python `__pycache__` artifacts, and the frontend tracks `tsconfig.tsbuildinfo`. Generated caches should generally be excluded from source control.

Test execution not verified

Tests exist across both repositories, but this review has not established passing results, coverage percentages, or deployment health.

## 7. Recommended engineering priorities

My recommended order for improving Reg-AI is:

1. Security validation: verify JWT claims, organization membership, tenant boundaries, role permissions, and local bypass restrictions.
2. Workflow reliability: make long-running regulatory and redline jobs resumable and idempotent, with durable progress tracking.
3. AI evaluation: establish repeatable tests for citation quality, gap detection, redline faithfulness, and hallucination prevention.
4. Refactoring: split the largest backend services and frontend components along clear domain boundaries.
5. Documentation and CI: align both READMEs with the live implementation, remove generated artifacts, and enforce lint, type, test, and migration checks.

## Final assessment

Reg-AI has an architecture capable of supporting a serious compliance SaaS product. Its distinguishing feature is not simply using LLMs to analyze documents; it is the integration of regulatory evidence, organizational policies, AI-assisted change generation, human governance, and audit history.

The largest remaining engineering challenge is demonstrating that these subsystems remain secure, consistent, reproducible, and reliable under production conditions.

I now have a source-grounded architectural understanding of both repositories that we can use for subsequent discussions, debugging, feature design, and code reviews in this project. This review made no repository modifications.
