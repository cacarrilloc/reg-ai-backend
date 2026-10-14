# Phase 1 Foundation

This backend is wired for the Phase 1 foundation services. Live cloud resources still need their project credentials in the deployment platforms.

## Supabase

- Create separate Supabase projects for development and test.
- Run `supabase db push` from this repository, or run `alembic upgrade head` against the Supabase Postgres connection string.
- The migration enables `pgcrypto` and `vector`, creates the 7 core tables, enables RLS, creates org-scoped policies, and provisions private Storage buckets:
  - `policy-documents`
  - `policy-redlines`
  - `approved-policies`
  - `context-documents`
- The migration also creates auxiliary `context_documents` and `document_chunks` tables for later V1 phases. These are not part of the 7 core workflow tables, but they are required for onboarding context and pgvector RAG retrieval.
- Store object keys as `<clerk_org_id>/<document-id>/<filename>` so Storage RLS can enforce org access.

## Clerk

Configure a Clerk JWT template with:

- `org_id`: the active Clerk organization ID.
- `metadata.role` or the configured `CLERK_ROLE_CLAIM`: one of `Master User`, `Developer User`, `Product Manager`, or `Admin User` for platform staff.
- `o.rol` or `org_role`: the active customer Organization role. The backend recognizes `Chief Compliance Officer` / `org:chief_compliance_officer` and `Nominated Team Member` / `org:nominated_team_member`.

Create the custom Clerk Organization roles `org:chief_compliance_officer` and
`org:nominated_team_member`, and add them to the Role Set used by client organizations.
`POST /api/v1/admin/client-orgs` assigns `org:chief_compliance_officer` to the submitted
`cco_user_id`; it requires `CLERK_SECRET_KEY` in addition to the normal Clerk JWT settings.

Set `AUTH_BYPASS_ENABLED=false` in Railway and other shared environments.

## Inngest

Set `INNGEST_APP_ID`, `INNGEST_EVENT_KEY`, and `INNGEST_SIGNING_KEY`. When those values are present, the FastAPI app registers the daily regulatory scrape function with the Inngest Python SDK.

## Observability

- `SENTRY_DSN` enables FastAPI Sentry instrumentation.
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, and optional `LANGFUSE_HOST` enable Langfuse trace creation from backend LLM/agent paths.
- `ANTHROPIC_OPUS_MODEL` (default `claude-opus-4-6`) for gap analysis, business-change assessment, and compliance reports; `ANTHROPIC_SONNET_MODEL` (default `claude-sonnet-4-6`) for redline drafting; plus `OPENAI_EMBEDDING_MODEL` and `EMBEDDING_DIMENSIONS` keep model routing environment-driven. Optional overrides: `ANTHROPIC_GAP_ANALYSIS_MODEL`, `ANTHROPIC_REDLINE_MODEL`.

## Railway

Deploy the backend from `railway.json`. See **`docs/railway-deployment.md`** for the complete Railway + Vercel integration guide and `deploy/railway.env.backend` for production variables.

Required Railway variables:

- `ENVIRONMENT=production`
- `AUTH_BYPASS_ENABLED=false`
- `DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_ACCESS_TOKEN`
- `CLERK_ISSUER`
- `CLERK_JWKS_URL`
- `CLERK_AUTHORIZED_PARTIES`
- `INNGEST_EVENT_KEY`
- `INNGEST_SIGNING_KEY`
- `SENTRY_DSN`
- `LANGFUSE_PUBLIC_KEY`
- `LANGFUSE_SECRET_KEY`


## Anthropic vs. OpenAI

There is a technical reason to use 2 different models so the billing is separate:
>Anthropic credit pays for Claude calls, while OpenAI API credit pays for embeddings. You could replace OpenAI embeddings with another provider later, but that requires coordinated changes to the embedding model, configured vector dimensions, document_chunks.embedding schema, and pgvector index.These two models to be used serve different functions:

**Anthropic Claude Sonnet 4.5:**
>Evaluates a business change and decides whether it should trigger policy review. This is used in [business_change_assessment.py](/Users/cacarrilloc/Documents/Internship/Code/reg-ai-backend/app/services/business_change_assessment.py).


**OpenAI text-embedding-3-large:**
>Converts policy text chunks into 3072-dimensional numeric vectors for storage and semantic retrieval in pgvector. This is used in [document_ingestion.py](/Users/cacarrilloc/Documents/Internship/Code/reg-ai-backend/app/services/document_ingestion.py).

**The design separates reasoning/generation from vector search. Claude receives the policy-review task; pgvector needs embedding vectors to find relevant policy passages later for retrieval and gap analysis.**
