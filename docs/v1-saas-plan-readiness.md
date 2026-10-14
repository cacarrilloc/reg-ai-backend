# V1 SaaS Plan Readiness

Source: `../reg-ai-frontend/reg-ai-v1-plan-Saas.html`.

## Delivery Model

- V1 is multi-tenant SaaS on Vercel, Railway, Supabase, Clerk, and Inngest.
- V1.5 dedicated cloud and V2 on-premise must use the same codebase, so tenancy, storage, jobs, and config are environment-driven.
- Every customer workflow is scoped by Clerk org ID and Supabase RLS.

## Phase Compatibility Matrix

| Phase | Plan requirement | Current readiness |
| --- | --- | --- |
| 1 Foundation | Next.js 15, FastAPI, Clerk, Supabase schema/RLS/storage, Inngest, Sentry/Langfuse/PostHog, CI | Implemented locally. Cloud project creation/deploy still requires credentials. |
| 2 Client data ingestion | Policy upload UI/API, PDF/DOCX parsing, chunking, OpenAI embeddings, pgvector chunks, context docs | Policy upload UI/API exist. Backend now parses PDF/DOCX, chunks text, calls OpenAI embeddings when configured, and persists policy chunks when the DB is configured. Optional context-document uploads remain implementation work. |
| 3 Regulatory monitoring | SEC API, FINRA/CFTC/NFA RSS, Inngest cron, publications feed | Implemented: async SEC data API and RSS scrapers, bounded SEC/FINRA request behavior, full-text extraction, durable publication deduplication and per-org outbox fan-out, four configured UTC Inngest crons, and a source-scoped dashboard feed. Deployment still requires source and Inngest environment values. |
| 4 Business change trigger | Business change entry, dated change, Sonnet assessment, event emission | Implemented: CCO/team entry flow, Claude Sonnet 4.5 structured assessment with deterministic and fail-safe fallbacks, persisted assessment metadata, and durable `business_change.flagged` outbox delivery into the same policy-change agent workflow. |
| 5 Gap analysis agent | LlamaIndex + pgvector retrieval, Opus gap prompt, impact scoring, GapAnalysis records | Implemented: tenant-scoped `document_chunks` pgvector retrieval exposed through LlamaIndex `VectorStoreIndex`, cached Opus 4 gap assessment, HIGH/MEDIUM/NONE scoring, idempotent persisted GapAnalysis records, and CCO filtering by impact and trigger. Apply the Phase 5 migration before use. |
| 6 Redline generation | Opus redline prompt, `docx-revisions`, citation comments, storage, pending revisions | Implemented: review endpoint creates idempotent pending `PolicyRevision` records from gap analyses, drafts replacement language with cached Opus 4 prompts or local fallback, writes true DOCX tracked insertions/deletions with citation comments, and uploads redlines to `policy-redlines`. |
| 7 CCO review workflow | Pending review dashboard, accept/reject, audit log, Resend notifications, reminders/escalation | Implemented: review queue dashboard with gap summaries/redline downloads, approve/reject decision endpoint, append-only audit log entries with actor and timestamp, approved DOCX promotion to canonical policy storage/version, Resend new-revision notifications, and Inngest cron jobs for SLA reminders plus CCO escalation. |
| 8 Audit/hardening | Audit trail UI, RLS audit, SOC 2 readiness, E2E tests | Audit page scaffold, RLS migration, and CI exist. SOC 2 process and full E2E tests require cloud setup and later-phase implementation. |

## Non-Negotiable Architecture Rules

- Do not expose Anthropic, OpenAI, Supabase service role, Resend, Inngest, or Clerk secret keys to the browser.
- Keep global regulatory publications separate from org-scoped generated outputs; scope policy, chunk, business-change, gap, revision, context, and audit data by `org_id`.
- Use `text-embedding-3-large` dimensionality (`vector(3072)`) unless `EMBEDDING_DIMENSIONS` and the migration are changed together.
- Preserve human-in-the-loop semantics: no generated redline becomes canonical without explicit CCO or delegated reviewer approval.
- Store files by `<clerk_org_id>/<document-id>/<filename>` to keep Supabase Storage RLS enforceable.
