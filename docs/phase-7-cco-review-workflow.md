# Phase 7 CCO Review Workflow

Phase 7 completes the human approval loop between generated redlines and canonical policy versions.

## Workflow

1. A gap analysis with impact creates an idempotent pending `PolicyRevision` redline.
2. The assigned reviewer from the organization's approval chain receives a Resend email with the gap summary and review dashboard link.
3. The review dashboard lists pending revisions with gap summary, reviewer routing, SLA/escalation state, and a signed redline download action.
4. A CCO or nominated reviewer records `APPROVED` or `REJECTED` with optional notes.
5. Each decision writes an `AuditLog` row containing `actor_id`, `timestamp`, `revision_id`, `policy_id`, `gap_analysis_id`, metadata, and notes.
6. Approval increments the policy version and stores the approved DOCX in the `approved-policies` Supabase Storage bucket as the new canonical active policy file.

## Review Queue SLA and escalation

Timers start when a revision enters the Review Queue (`review_requested_at`), not when the draft row was first created.

| Signal | Threshold | Recipient | Stamp |
|--------|-----------|-----------|-------|
| **SLA reminder** | Platform `SLA_REMINDER_DAYS` (default **3**) | Assigned reviewer, or CCO if none assigned | `sla_reminded_at` (may re-send after 24h) |
| **Escalation** | Org `escalation_days` (Settings; default **7**) | CCO (`approval_chain.cco_email`) | `escalated_at` (once) |

Configure CCO name/email and escalation days under **Settings → Organization structure and approval authorities**. Assign a reviewer from the Review Queue (or keep CCO as the fallback contact).

## Scheduled Jobs

- `review-sla-reminder` runs on `SLA_REMINDER_CRON` (default `0 9 * * *`) and re-notifies after `SLA_REMINDER_DAYS` with no decision.
- `review-escalation` runs on `ESCALATION_CRON` (default `0 10 * * *`) and notifies the CCO when a pending revision exceeds the organization's `escalation_days`.

Both jobs only operate on `PENDING` revisions with `review_requested_at` set, and stamp `sla_reminded_at` or `escalated_at` after a successful Resend send. They require Inngest keys (`INNGEST_EVENT_KEY`, `INNGEST_SIGNING_KEY`) and Resend (`RESEND_API_KEY`).
