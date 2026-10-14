# Phase 3 Regulatory Monitoring

## Sources

The monitoring service runs all external I/O through `httpx.AsyncClient`.

- SEC defaults to official RSS feeds (`https://www.sec.gov/news/pressreleases.rss`, `https://www.sec.gov/news/speeches-statements.rss`) via `SEC_NEWSROOM_URLS`. The HTML newsroom (`/newsroom`) often returns 503 under load, so RSS is preferred. Scrapes send the configured `SEC_USER_AGENT`.
- FINRA prefers official RSS feeds (`http://feeds.finra.org/FINRANotices`, `http://feeds.finra.org/FINRANews`) via `FINRA_RSS_URLS`. HTTPS against `feeds.finra.org` often fails TLS, so the scraper falls back across HTTP/HTTPS candidates. Already-stored URLs are skipped. By default (`REGULATORY_FETCH_FULL_TEXT=false`) only RSS summaries are stored — FINRA notice HTML pages return 429 aggressively. When full-text is enabled, article fetches share a process-wide limiter (`FINRA_REQUESTS_PER_SECOND`) and a 429 pauses remaining full-text fetches for that run.
- CFTC defaults to its general press-release RSS (`https://www.cftc.gov/RSS/RSSGP/rssgp.xml`). The HTML PressRoom hub is not scrapeable (Drupal News & Events with no release links). NFA's feed catalogue provides several selectable feeds, so production config must explicitly choose the relevant URLs via `NFA_RSS_URLS`. When an RSS item has an empty description (common for CFTC), the scraper fetches that article page once so reference docs still have embeddable text.

Each feed is bounded by `REGULATORY_MAX_ITEMS_PER_FEED`, uses `REGULATORY_REQUEST_TIMEOUT_SECONDS`, and retries transient HTTP failures up to `REGULATORY_HTTP_MAX_ATTEMPTS` times with exponential backoff (honoring `Retry-After` on 429 responses). Soft scrapers use fewer listing retries so a dead HTML newsroom does not stall uploads. Concurrent ingest jobs are serialized in-process to avoid stampeding regulator hosts.

## Persistence

`regulatory_publications` has a unique `(source, url)` index. New records are persisted and embedded for reference-policy sync. Per-organization gap analysis is owned by the `regulatory-compliance-workflow` cron (not a `publication.created` outbox). A no-op Inngest handler remains registered for any legacy `publication.created` events.

## Production Configuration

Set a truthful contact address in `SEC_USER_AGENT`; the SEC expects automated clients to identify themselves. Populate `NFA_RSS_URLS` before enabling their crons. Configure `INNGEST_EVENT_KEY` and `INNGEST_SIGNING_KEY` in Railway, apply migration `20260622_0004`, and let Inngest synchronize functions from `/api/v1/inngest`. Set `REGULATORY_FETCH_FULL_TEXT=true` only if you need full HTML article bodies and can tolerate slower, rate-limited scrapes.

## Internal Policy Compliance Cron

`regulatory-compliance-workflow` runs twice daily (`REGULATORY_COMPLIANCE_WORKFLOW_CRON`, default `TZ=America/New_York 0 4,14 * * *` — 4:00 AM and 2:00 PM Eastern). For each organization it:

1. Scrapes **FINRA, SEC, and CFTC** and refreshes Regulations reference docs **only when content changed** (hash check before OpenAI embeds)
2. Iterates **all active Internal Policy documents**
3. Always runs sentence-level RULE / GAP / FIX gap analysis for every Internal Policy and overwrites that policy's single Gap Analysis row

Gap analysis is also triggered on every Internal Policy upload. Redlines are **not** auto-enqueued; they still require **Request Review** from the Gap Analysis table.

The manual ingestion endpoint is for internal operational use. Customer users only read `GET /api/v1/regulatory/publications`, filtered to their configured sources.
