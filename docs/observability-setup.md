# Observability setup (Sentry, Resend, Langfuse, PostHog)

Reg-AI uses four optional observability services. The app works without them, but each adds production visibility once configured.

| Service | Runs on | Purpose |
|---------|---------|---------|
| **Sentry** | Railway + Vercel | Error and performance monitoring |
| **Resend** | Railway | Email notifications for review workflow |
| **Langfuse** | Railway | LLM trace logging (gap analysis, redlines, etc.) |
| **PostHog** | Vercel | Product analytics and page views |

Check configuration:

```bash
curl -s https://<railway-backend>/api/v1/health | python3 -m json.tool
```

Look for `sentry_configured`, `langfuse_configured`, `resend_configured`, `security_headers_enabled`, `rate_limit_enabled`, and `ip_allowlist_enabled`.

Auth failures also emit Sentry warnings (`authentication_failed`). Create a Sentry alert rule on that message/tag for on-call coverage. Clerk webhook intake is documented in `docs/security-hardening.md`.

---

## 1. Sentry

**What it does:** Captures unhandled exceptions and slow requests in production.

### Create project

1. [sentry.io](https://sentry.io) → **Create project**
2. Create one project for **FastAPI** (backend) and one for **Next.js** (frontend), or reuse one DSN for both during development.

### Railway (backend)

```env
SENTRY_DSN=https://<key>@<org>.ingest.us.sentry.io/<project-id>
SENTRY_TRACES_SAMPLE_RATE=0.2
SENTRY_PROFILES_SAMPLE_RATE=0.0
```

### Vercel (frontend)

```env
NEXT_PUBLIC_SENTRY_DSN=https://<key>@<org>.ingest.us.sentry.io/<project-id>
NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=0.2
SENTRY_DSN=https://<key>@<org>.ingest.us.sentry.io/<project-id>
SENTRY_ORG=your-sentry-org-slug
SENTRY_PROJECT=reg-ai-frontend
SENTRY_AUTH_TOKEN=<token-for-source-maps-in-CI>
```

### Verify (local)

1. Set `SENTRY_DSN` in `.env` (do **not** hardcode the DSN in Python).
2. Restart the backend.
3. Open `http://localhost:8000/sentry-debug` — this route exists only when `ENVIRONMENT` is not `production`.
4. In Sentry → **Issues**, confirm a new `ZeroDivisionError` appears within a minute.

On Railway production, real errors are captured automatically; `/sentry-debug` is disabled.

---

## 2. Resend

**What it does:** Sends transactional emails when:

- A policy revision is ready for review
- SLA reminders fire for pending reviews
- Reviews escalate to the CCO
- Approve/reject decisions are recorded

### Setup

1. [resend.com](https://resend.com) → **API Keys** → create key
2. Verify domain `alerts.ipxenterprises.com` (DKIM / SPF / MX) in Resend **Domains**
3. Use a From address on that domain

### Local / Railway (backend)

```env
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=Reg-AI <notifications@alerts.ipxenterprises.com>
```

Sandbox fallback (only delivers to your Resend account email):

```env
RESEND_FROM_EMAIL=Reg-AI <notifications@alerts.ipxenterprises.com>
```

Without `RESEND_API_KEY`, emails are skipped with `"status": "skipped"` — the rest of the workflow continues.

---

## 3. Langfuse

**What it does:** Logs LLM workflow events and Anthropic generations for:

- Gap analysis
- Business change assessment and analysis
- Redline drafting
- Compliance reports

View traces in the Langfuse dashboard under **Traces**.

### What gets traced

| Layer | Trace name pattern | Notes |
|-------|-------------------|--------|
| API requests | `api.get.gap-analyses`, etc. | Root span per request; tagged by feature |
| Gap analysis | `gap-analysis` | Anthropic generations + workflow steps |
| Policy redlines | `policy-redline` | Anthropic generations |
| Policy upload | `policy-upload` | Embeddings via Langfuse OpenAI wrapper |
| Internal policy workflow | `internal-policy-workflow` | Cron + upload pipeline |
| Review decisions | `review-decision` | Includes `cco_decision` score (1=approved, 0=rejected) |
| Business changes | `business-change-*` | Assessment + analysis |
| Compliance reports | `compliance-report` | Report generation |

Sessions are grouped by **org_id**; **user_id** is the Clerk user when available.

### Setup

1. [cloud.langfuse.com](https://cloud.langfuse.com) or [us.cloud.langfuse.com](https://us.cloud.langfuse.com) → **Settings → API Keys**
2. Copy **Public Key** and **Secret Key** from the same project (they must be a matching pair)
3. Set `LANGFUSE_HOST` to the region shown in Langfuse settings

### Railway (backend)

```env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

- US cloud: `LANGFUSE_HOST=https://us.cloud.langfuse.com`
- EU cloud: `LANGFUSE_HOST=https://cloud.langfuse.com`

After restart, `GET /api/v1/health` should report `langfuse_configured: true`. Traces appear under **Tracing → Traces**; filter environment to `local` for local backends (or your Railway `ENVIRONMENT` value).

---

## 4. PostHog

**What it does:** Tracks page views and identifies signed-in Clerk users for product analytics.

### Setup

1. [posthog.com](https://posthog.com) → **Project settings**
2. Copy **Project API Key** and note your **Host** (US: `https://us.i.posthog.com`)

### Vercel (frontend)

```env
NEXT_PUBLIC_POSTHOG_KEY=phc_...
NEXT_PUBLIC_POSTHOG_HOST=https://us.i.posthog.com
```

Redeploy Vercel. Open **Settings → Administration** in the app to see PostHog status.

Signed-in users are identified with Clerk `userId` and active `org_id`.

---

## Quick copy: Railway observability block

Add to Railway **Variables → Raw Editor** (alongside your existing production vars):

```env
SENTRY_DSN=
SENTRY_TRACES_SAMPLE_RATE=0.2
RESEND_API_KEY=
RESEND_FROM_EMAIL=Reg-AI <notifications@alerts.ipxenterprises.com>
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
```

## Quick copy: Vercel observability block

```env
NEXT_PUBLIC_POSTHOG_KEY=
NEXT_PUBLIC_POSTHOG_HOST=https://us.i.posthog.com
NEXT_PUBLIC_SENTRY_DSN=
NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE=0.2
SENTRY_DSN=
SENTRY_ORG=
SENTRY_PROJECT=
```

---

## Verify after deploy

```bash
# Backend observability flags
curl -s https://<railway>/api/v1/health

# Trigger Langfuse: run a gap analysis or business change assessment, then check Langfuse Traces

# Trigger Resend: request review on a gap analysis with a reviewer email in the approval chain

# Trigger PostHog: visit the Vercel app while signed in, check PostHog Live events

# Trigger Sentry: optional test endpoint or throw in dev — production errors auto-report
```

See also: [`docs/railway-deployment.md`](railway-deployment.md) for full Railway + Vercel deployment.
