# Phase 8 Audit Trail and Hardening

## Implemented Scope

- Review Queue UI loads pending policy revisions, opens signed redline download URLs, and records approve/reject decisions.
- Audit Trail UI loads a full per-policy chain: trigger, gap analysis, redline revision, reviewer, decision, notes, and date.
- Backend exposes `GET /api/v1/reviews/audit-chains` for the audit trail.
- Backend migration `20260627_0008` backfills and enforces `policy_revisions.org_id`, `policy_revisions.policy_id`, `audit_logs.gap_analysis_id`, and `audit_logs.notes`.
- Supabase RLS is re-enabled and forced on customer data tables, with regulator publication reads narrowed to the active organization's source scope.
- Audit logs are append-only at the database layer through no-update and no-delete triggers.
- Tracked `.env` and `.env.backend` values are placeholders only.

## RLS Hardening Notes

Customer-visible application queries are scoped by `user.org_id` before data leaves the FastAPI backend.
Supabase policies also enforce tenant scope for direct authenticated access:

- `organizations`: `clerk_org_id = current_clerk_org_id()`
- `policies`, `context_documents`, `document_chunks`, `business_changes`, `gap_analyses`, `policy_revisions`, `audit_logs`: `org_id = current_clerk_org_id()`
- `regulatory_publications`: internal users or active organization's configured source list
- `business_change_event_deliveries`: internal users or matching `org_id`
- Storage objects: first path segment must match the active Clerk organization ID

Internal service-role jobs still perform cross-tenant work where the product requires it, such as business-change fan-out across matching organizations. Those jobs must continue to run only server-side.

## Env Audit Checklist

The repository can confirm that tracked examples do not contain live credentials. Hosted environment confirmation still requires Vercel and Railway dashboard or CLI access.

Vercel frontend expected variables:

- `NEXT_PUBLIC_APP_ENV`
- `NEXT_PUBLIC_API_BASE_URL`
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`
- `NEXT_PUBLIC_CLERK_SIGN_IN_URL`
- `NEXT_PUBLIC_CLERK_SIGN_UP_URL`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL`
- `NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL`
- `NEXT_PUBLIC_CLERK_JWT_TEMPLATE`
- `NEXT_PUBLIC_POSTHOG_KEY`
- `NEXT_PUBLIC_POSTHOG_HOST`
- `NEXT_PUBLIC_SENTRY_DSN`
- `NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE`
- `SENTRY_DSN`
- `SENTRY_ORG`
- `SENTRY_PROJECT`
- `SENTRY_AUTH_TOKEN`

Vercel variables that must not be present with `NEXT_PUBLIC_`:

- Clerk secret keys
- Supabase service role keys
- Supabase secret keys
- Anthropic or OpenAI keys
- Resend keys
- Inngest signing or event keys
- Database URLs
- Railway tokens

Railway backend expected variables:

- `APP_NAME`
- `ENVIRONMENT`
- `API_V1_PREFIX`
- `CORS_ORIGINS`
- `DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORAGE_BUCKETS`
- `FRONTEND_URL`
- `CLERK_ISSUER`
- `CLERK_JWKS_URL`
- `CLERK_AUDIENCE`
- `CLERK_AUTHORIZED_PARTIES`
- `CLERK_ORG_CLAIM`
- `CLERK_ROLE_CLAIM`
- `CLERK_SECRET_KEY`
- `AUTH_BYPASS_ENABLED=false`
- `ALLOWED_IPS` / `ALLOWED_IP_CIDRS` (staging recommended)
- `SECURITY_HEADERS_ENABLED=true`
- `RATE_LIMIT_ENABLED=true`
- `RATE_LIMIT_REQUESTS_PER_MINUTE`
- `RATE_LIMIT_AUTH_FAILURES_PER_MINUTE`
- `CLERK_WEBHOOK_SECRET`
- `INNGEST_APP_ID`
- `INNGEST_EVENT_KEY`
- `INNGEST_SIGNING_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `RESEND_API_KEY`
- `SENTRY_DSN`
- `SEC_USER_AGENT`

## SOC 2 Readiness Notes

Code-level readiness now covers:

- Audit logging for redline approval and rejection decisions.
- Append-only audit log enforcement in Postgres.
- Clerk authenticated access with organization-scoped roles (beyond Admin/User).
- API handlers that derive tenant scope from the authenticated user instead of request body org IDs.
- Private Supabase buckets and org-prefixed storage paths.
- Sentry/PostHog/Langfuse support for operational monitoring without server secrets in client code.
- HTTP security headers on API and Next.js responses.
- Inbound per-IP rate limiting and auth-failure throttling.
- Optional IP allowlisting for staging/test APIs (`ALLOWED_IPS` / `ALLOWED_IP_CIDRS`).
- Failed authentication monitoring (structured logs + Sentry) and Clerk webhook intake.
- Staging environment runbook: `docs/staging-environment.md`.
- Security operator guide: `docs/security-hardening.md`.

Evidence still required outside code:

- Production Vercel and Railway env screenshots or CLI exports reviewed by a second person.
- Supabase project region and backup retention evidence for data residency.
- Access review for Supabase, Vercel, Railway, Inngest, Resend, Sentry, PostHog, Anthropic, and OpenAI.
- Signed annual acknowledgment of policies under `docs/policies/`.
- Production audit log retention period and restore test evidence.
- Supabase Enterprise SSO / SAML configuration screenshots for customer IdP onboarding.
- Sentry alert routing for `authentication_failed` events.

## Phase 8 E2E Runs

Live regulatory trigger:

1. Configure a dummy firm organization with source scope `["FINRA"]`.
2. Upload at least one DOCX WSP policy with supervisory procedures text.
3. Run `POST /api/v1/regulatory/ingest` as an internal user (or wait for the compliance cron).
4. Confirm FINRA publications are persisted and Regulations reference docs refresh.
5. Let the `regulatory-compliance-workflow` cron create gap analyses for active Internal Policies.
6. Generate a redline from a HIGH or MEDIUM gap with `POST /api/v1/reviews/gap-analyses/{gap_analysis_id}/redline`.
7. Approve the revision from `/reviews`.
8. Confirm `/audit` shows trigger, gap, redline, reviewer, decision, and date.

Business-change trigger:

1. Log a manual business change from `/business-changes`.
2. Confirm the assessment requires review and emits the workflow event.
3. Let the `business_change.flagged` workflow create gap analyses.
4. Generate a redline, approve it, and confirm the audit chain from `/audit`.

## Dummy Firm POC Profile

Use this profile for the POC run:

- Firm: Hudson River Securities LLC
- Type: broker-dealer
- Location: New York
- Size: 150 brokers
- Compliance team: 4 people
- Exam due: Q3 2025
- Source scope: FINRA, SEC
- Required seed documents: WSP, electronic communications policy, outside business activities policy, new product approval procedure
