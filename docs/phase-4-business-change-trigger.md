# Phase 4 Business-Change Trigger

The CCO dashboard submits `change_type`, `description`, and optional `change_date` to `POST /api/v1/business-changes`. CCOs and nominated team members can create changes; internal Master, Developer, and Admin users can also operate the endpoint.

The backend evaluates the record against the organization's `regulatory_scope`. When `ANTHROPIC_API_KEY` is present, the request uses `ANTHROPIC_OPUS_MODEL` (default `claude-opus-4-6`) at zero temperature and expects only a JSON result containing `triggers_review` and a concise rationale. The business-change description is serialized as user data, separate from the fixed system prompt. Display-style Opus aliases such as `claude-opus-4` are normalized to the supported API model ID.

No Anthropic key is required for local development. The deterministic fallback flags types in `BUSINESS_CHANGE_REVIEW_TYPES`, which defaults to `NEW_PRODUCT,NEW_PROCESS,NEW_HIRE`. A provider error or malformed response fails safe: the change requires a human review and the record is marked `FAIL_SAFE`.

After intake assessment, the business-change workflow:

1. Loops through every ACTIVE Internal Policy in the Policy Library and asks the LLM whether the change wording matches that document.
2. **Match found:** runs sentence-level RULE / GAP / FIX Gap Analysis against the matched Internal Policy and opens a Regulation Gap Analysis row. Request Review then builds a tracked redline on that matched policy (same RULE/GAP/FIX comment format as other policy updates).
3. **No match:** creates a brand-new draft Internal Policy document for the business change, opens a Gap Analysis row for CCO review/approval, and does **not** create a redline.
4. Continues to generate the existing regulatory analysis DOCX for the business change.

The final decision, rationale, assessment mode, model, and timestamp are stored on `business_changes`. A flagged change creates one durable `business_change_event_deliveries` outbox record before delivery. A successful send marks that record with the Inngest event ID and timestamp; a transient send failure leaves it pending for retry. The `business-change-policy-change-agent` Inngest function maps `business_change.flagged` to `run_business_change_workflow`.

Apply the migration before testing against a persistent database:

```bash
alembic upgrade head
```
