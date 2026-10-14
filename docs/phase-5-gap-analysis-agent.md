# Phase 5 Gap Analysis Agent

`business_change.flagged` enters the shared Inngest policy-change workflow. Regulatory publication gap analysis is owned by the `regulatory-compliance-workflow` cron rather than `publication.created`. Phase 5 resolves the trigger, embeds its text with the configured OpenAI embedding model, and queries the existing `document_chunks` pgvector column through a read-only LlamaIndex `VectorStoreIndex`. The adapter applies `org_id`, active-policy, and embedded-chunk filters inside the database query before ranking chunks.

For each retrieved policy document, the service sends the trigger and a bounded set of relevant excerpts to Anthropic's direct SDK using `ANTHROPIC_GAP_ANALYSIS_MODEL` when set, otherwise `ANTHROPIC_OPUS_MODEL` (default `claude-opus-4-6`). Display-style values such as `claude-opus-4` are normalized to the supported API model ID. The static system prompt is sent as an Anthropic ephemeral cache block, so repeated runs can use prompt caching. The model must return only `HIGH`, `MEDIUM`, or `NONE`, a plain-English CCO summary, and trigger-backed citations. Unknown scores normalize to `NONE`; citations not tied to the supplied trigger are discarded.

Redline drafting after Request Review uses `ANTHROPIC_REDLINE_MODEL` when set, otherwise `ANTHROPIC_SONNET_MODEL` (default `claude-sonnet-4-6`), keeping volume drafting on the faster/cheaper model while gap judgments stay on Opus.

Provider-less local development uses a deliberately labeled lexical fallback. A production embedding-retrieval failure stops the run for retry instead of silently claiming an analysis was completed. An unavailable or malformed Opus response produces a human-review fallback result and never creates or approves a policy revision.

Each record stores its `trigger_type`, `trigger_id`, policy, impact score, citation objects, retrieval mode/chunk IDs/scores, analysis mode/model, and timestamp. The `(org_id, trigger_type, trigger_id, policy_id)` unique index makes retries idempotent. CCOs can read their organization’s results at:

```text
GET /api/v1/gap-analyses?impact_score=HIGH&trigger_type=publication
```

Run the database migration before enabling the worker:

```bash
alembic upgrade head
```

Generated results remain decision support. A CCO or delegated reviewer must evaluate and approve any eventual policy amendment.
