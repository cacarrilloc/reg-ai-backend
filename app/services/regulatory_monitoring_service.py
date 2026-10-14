from __future__ import annotations

import asyncio
import calendar
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import feedparser
import httpx
import structlog
from bs4 import BeautifulSoup
from sqlalchemy import not_, or_, tuple_
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session, defer

from app.core.config import Settings, get_settings
from app.models.core import DocumentChunk, GapAnalysis, Organization, RegulatoryPublication
from app.schemas.regulatory import RegulatoryIngestResponse, RegulatoryPublicationResponse
from app.services.policy_types import REGULATORY_PUBLICATION_CHUNK_TYPE
from app.services.rate_limiter import AsyncRateLimiter

logger = structlog.get_logger(__name__)

SEC_SOURCE = "SEC"
FINRA_SOURCE = "FINRA"
CFTC_SOURCE = "CFTC"
NFA_SOURCE = "NFA"
ALL_SOURCES = frozenset({SEC_SOURCE, FINRA_SOURCE, CFTC_SOURCE, NFA_SOURCE})
PREFERRED_SOURCE_ORDER = (SEC_SOURCE, FINRA_SOURCE, CFTC_SOURCE, NFA_SOURCE)
MAX_PUBLICATION_TEXT_LENGTH = 100_000
# Soft listing fetches should fail fast — HTML newsrooms (SEC) often return 503.
FAIL_SOFT_LISTING_ATTEMPTS = 2
_finra_limiter: AsyncRateLimiter | None = None
_sec_newsroom_limiter: AsyncRateLimiter | None = None
_ingest_lock = asyncio.Lock()
FINRA_ARTICLE_PATH_PREFIXES = (
    "/media-center/newsreleases/",
    "/rules-guidance/notices/",
)
FINRA_SECTION_PATH_PREFIXES = (
    "/media-center/newsreleases",
    "/rules-guidance/notices",
)
CFTC_ARTICLE_PATH_PREFIXES = ("/PressRoom/PressReleases/",)
# Require a release slug after the folder so /PressRoom/PressReleases/index.htm
# on the News & Events hub is not treated as a publication.
CFTC_ARTICLE_PATH_PATTERN = re.compile(r"^/PressRoom/PressReleases/\d", re.IGNORECASE)
CFTC_SECTION_PATH_PREFIXES = ("/PressRoom/PressReleases",)
SEC_NEWSROOM_ARTICLE_PATH_PREFIXES = (
    "/newsroom/press-releases/",
    "/newsroom/whats-new/",
    "/newsroom/speeches-statements/",
)
SEC_NEWSROOM_SECTION_PATH_PREFIXES = (
    "/newsroom/press-releases",
    "/newsroom/whats-new",
    "/newsroom/speeches-statements",
)


@dataclass(frozen=True)
class ScrapedPublication:
    source: str
    title: str
    url: str
    published_at: datetime | None
    raw_text: str | None


@dataclass(frozen=True)
class IngestResult:
    publications: list[ScrapedPublication]
    errors: list[str]


def build_sec_request_headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "User-Agent": settings.sec_user_agent,
        "Accept": "application/json,text/html,application/xhtml+xml,application/rss+xml",
    }


_ISSUER_EDGAR_TITLE_RE = re.compile(
    r':\s*(?:form\s+)?(?:144|10-k|10-q|8-k|20-f|6-k|[345]|s-1|s-3|13f[- ]?hr|sc\s*13[dg])\s*$',
    re.I,
)


def is_sec_edgar_filing_url(url: str | None) -> bool:
    """True for company EDGAR archive filings (not SEC newsroom / rules pages)."""
    lowered = str(url or "").lower()
    return "/archives/edgar/" in lowered or "xsl144" in lowered


def is_issuer_filing_publication(*, url: str | None = None, title: str | None = None) -> bool:
    """True for issuer disclosures such as Form 144 / 10-K, not regulator notices."""
    if is_sec_edgar_filing_url(url):
        return True
    lowered_url = str(url or "").lower()
    if "data.sec.gov" in lowered_url and "/submissions/" in lowered_url:
        return True
    text = str(title or "").strip()
    if _ISSUER_EDGAR_TITLE_RE.search(text):
        return True
    return bool(re.search(r'\bform\s+144\b', text, re.I))


def _exclude_issuer_filings_clause():
    return not_(
        or_(
            RegulatoryPublication.url.ilike("%/archives/edgar/%"),
            RegulatoryPublication.url.ilike("%xsl144%"),
            RegulatoryPublication.title.ilike("%: 144%"),
            RegulatoryPublication.title.ilike("%form 144%"),
        )
    )


def regulatory_sources() -> list[dict[str, str]]:
    """Describe the configured regulator integrations without exposing secrets."""
    return [
        {
            "source": SEC_SOURCE,
            "mode": "sec.gov/newsroom+rss",
            "user_agent": build_sec_request_headers()["User-Agent"],
        },
        {"source": FINRA_SOURCE, "mode": "finra.org/media-center+rss"},
        {"source": CFTC_SOURCE, "mode": "cftc.gov/PressRoom+rss"},
        {"source": NFA_SOURCE, "mode": "rss-html"},
    ]


def get_finra_limiter(settings: Settings | None = None) -> AsyncRateLimiter:
    """Process-wide FINRA limiter so concurrent Inngest jobs do not stampede www.finra.org."""
    global _finra_limiter
    settings = settings or get_settings()
    desired = 1.0 / max(settings.finra_requests_per_second, 0.05)
    if _finra_limiter is None or abs(_finra_limiter.minimum_interval - desired) > 1e-9:
        _finra_limiter = AsyncRateLimiter(
            requests_per_second=max(settings.finra_requests_per_second, 0.05)
        )
    return _finra_limiter


def get_sec_newsroom_limiter(settings: Settings | None = None) -> AsyncRateLimiter:
    """Throttle SEC newsroom HTML fetches; www.sec.gov returns 503 when flooded."""
    global _sec_newsroom_limiter
    settings = settings or get_settings()
    # Keep newsroom slower than data.sec.gov's 10 rps filing API budget.
    rate = max(settings.sec_newsroom_requests_per_second, 0.05)
    desired = 1.0 / rate
    if _sec_newsroom_limiter is None or abs(_sec_newsroom_limiter.minimum_interval - desired) > 1e-9:
        _sec_newsroom_limiter = AsyncRateLimiter(requests_per_second=rate)
    return _sec_newsroom_limiter


def load_existing_publication_urls(
    db: Session,
    sources: Iterable[str],
) -> dict[str, set[str]]:
    """Return already-persisted publication URLs keyed by regulator source."""
    selected = sorted({str(source).strip().upper() for source in sources if str(source).strip()})
    if not selected:
        return {}
    rows = (
        db.query(RegulatoryPublication)
        .filter(RegulatoryPublication.source.in_(selected))
        .all()
    )
    from app.services.publication_summary_service import publication_text_is_thin

    known: dict[str, set[str]] = {source: set() for source in selected}
    for publication in rows:
        source = publication.source
        url = publication.url
        if source not in known or not url:
            continue
        body = (publication.raw_text or '').strip()
        if not body or publication_text_is_thin(body, publication.title):
            # Thin RSS blurbs must be re-fetched so the feed can show a real summary.
            continue
        known[source].add(str(url))
    return known


async def ingest_regulatory_feeds(
    db: Session | None = None,
    sources: Iterable[str] | None = None,
    *,
    org_id: str | None = None,
) -> RegulatoryIngestResponse:
    """Fetch selected sources, persist new records, and sync reference policies.

    Serialized in-process so concurrent upload/cron jobs do not stampede the
    same regulator hosts (which triggers 429/503).

    Prefer ``db=None`` so this function opens short-lived sessions around DB work
    and does **not** hold a pool connection across multi-minute HTTP scrapes or
    OpenAI embedding calls. Passing ``db`` keeps the legacy path for unit tests.
    """
    async with _ingest_lock:
        return await _ingest_regulatory_feeds_unlocked(
            db, sources, org_id=org_id
        )


async def _ingest_regulatory_feeds_unlocked(
    db: Session | None,
    sources: Iterable[str] | None,
    *,
    org_id: str | None = None,
) -> RegulatoryIngestResponse:
    settings = get_settings()
    selected_sources = normalized_sources(sources)
    timeout = httpx.Timeout(settings.regulatory_request_timeout_seconds)

    # Prefer self-managed sessions so callers (upload/sync) can release the pool
    # before the long scrape. A passed ``db`` is only used when SessionLocal is
    # unavailable (tests / no-DB mode).
    from app.db.session import SessionLocal

    use_owned_sessions = db is None and SessionLocal is not None

    known_urls_by_source: dict[str, set[str]] = {}
    if use_owned_sessions:
        from app.db.session import platform_session_scope

        with platform_session_scope(commit=False) as scoped:
            known_urls_by_source = load_existing_publication_urls(
                scoped, selected_sources
            )
    elif db is not None:
        known_urls_by_source = load_existing_publication_urls(db, selected_sources)

    # HTTP only — no SQLAlchemy connection checked out.
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": settings.sec_user_agent},
        follow_redirects=True,
    ) as client:
        scraped = await collect_regulatory_publications(
            client,
            settings,
            selected_sources,
            known_urls_by_source=known_urls_by_source,
        )

    if use_owned_sessions:
        return await _persist_embed_and_sync_owned(
            scraped=scraped,
            selected_sources=selected_sources,
            settings=settings,
            org_id=org_id,
        )

    if db is None:
        return RegulatoryIngestResponse(
            status="completed_without_persistence",
            sources=sorted(selected_sources),
            fetched_count=len(scraped.publications),
            created_count=0,
            duplicate_count=0,
            emitted_event_count=0,
            errors=scraped.errors,
        )

    return await _persist_embed_and_sync_on_session(
        db,
        scraped=scraped,
        selected_sources=selected_sources,
        settings=settings,
    )


async def _persist_embed_and_sync_on_session(
    db: Session,
    *,
    scraped: IngestResult,
    selected_sources: set[str] | list[str] | frozenset[str],
    settings: Settings,
) -> RegulatoryIngestResponse:
    """Legacy finalize path used when the caller already owns a Session (tests)."""
    created, duplicates = persist_new_publications(db, scraped.publications)
    pending_embed = list(created)
    pending_embed.extend(list_publications_missing_embeddings(db, selected_sources))
    seen_ids: set[str] = set()
    to_embed: list[RegulatoryPublication] = []
    for publication in pending_embed:
        if publication.id in seen_ids:
            continue
        seen_ids.add(publication.id)
        to_embed.append(publication)
    embedded_count = await embed_created_publications(db, to_embed, settings)
    from app.services.publication_summary_service import (
        fill_publication_feed_summaries,
        list_publications_missing_feed_summaries,
    )

    summary_targets = list(created)
    summary_targets.extend(list_publications_missing_feed_summaries(db))
    await fill_publication_feed_summaries(db, summary_targets, settings)
    from app.services.regulatory_reference_policy_service import (
        sync_regulatory_reference_policies,
    )

    reference_policy_sync_count = await sync_regulatory_reference_policies(
        db,
        selected_sources,
        settings,
    )
    return RegulatoryIngestResponse(
        status="completed",
        sources=sorted(selected_sources),
        fetched_count=len(scraped.publications),
        created_count=len(created),
        duplicate_count=duplicates,
        embedded_count=embedded_count,
        reference_policy_sync_count=reference_policy_sync_count,
        emitted_event_count=0,
        errors=scraped.errors,
    )


async def _persist_embed_and_sync_owned(
    *,
    scraped: IngestResult,
    selected_sources: set[str] | list[str] | frozenset[str],
    settings: Settings,
    org_id: str | None,
) -> RegulatoryIngestResponse:
    """Phase A: plan under a short session; release before OpenAI embeds."""
    from app.db.session import platform_session_scope
    from app.services.document_ingestion import chunk_publication_text, generate_embeddings
    # sync import deferred to call site below

    with platform_session_scope() as scoped:
        created, duplicates = persist_new_publications(scoped, scraped.publications)
        pending_embed = list(created)
        pending_embed.extend(
            list_publications_missing_embeddings(scoped, selected_sources)
        )
        seen_ids: set[str] = set()
        snapshots: list[dict[str, Any]] = []
        for publication in pending_embed:
            if publication.id in seen_ids:
                continue
            seen_ids.add(publication.id)
            if publication.embedding_chunk_ids:
                continue
            snapshots.append(
                {
                    "id": publication.id,
                    "title": publication.title,
                    "raw_text": publication.raw_text,
                    "source": publication.source,
                    "url": publication.url,
                }
            )
        created_count = len(created)

    prepared: list[tuple[dict[str, Any], list[Any], list[list[float]]]] = []
    if settings.openai_api_key:
        for snapshot in snapshots:
            chunks = chunk_publication_text(snapshot["title"], snapshot["raw_text"])
            if not chunks:
                continue
            embeddings = await generate_embeddings(chunks, settings, org_id=None)
            if not embeddings:
                continue
            prepared.append((snapshot, chunks, embeddings))

    embedded_count = 0
    with platform_session_scope() as scoped:
        embedded_count = persist_prepared_publication_embeddings(
            scoped, prepared, settings
        )
        from app.services.publication_summary_service import (
            feed_summary_needs_refresh,
            list_publications_missing_feed_summaries,
        )

        summary_targets = (
            scoped.query(RegulatoryPublication)
            .filter(RegulatoryPublication.id.in_(list(seen_ids)))
            .all()
            if seen_ids
            else []
        )
        summary_targets.extend(list_publications_missing_feed_summaries(scoped))
        # Release is after this block; LLM calls must not hold the session.
        summary_snapshots = [
            {
                'id': publication.id,
                'title': publication.title,
                'source': publication.source,
                'url': publication.url,
                'raw_text': publication.raw_text,
                'feed_summary': publication.feed_summary,
            }
            for publication in summary_targets
            if feed_summary_needs_refresh(publication.feed_summary)
        ]

    if summary_snapshots:
        from app.services.publication_summary_service import (
            generate_publication_feed_summary,
            should_replace_feed_summary,
        )

        updates: list[tuple[str, str]] = []
        for snapshot in summary_snapshots:
            publication_id = snapshot['id'] or ''
            title = snapshot['title'] or ''
            source = snapshot['source'] or ''
            url = snapshot['url'] or ''
            if not publication_id or not title or not source or not url:
                continue
            summary = await generate_publication_feed_summary(
                title=title,
                source=source,
                url=url,
                raw_text=snapshot['raw_text'],
                settings=settings,
            )
            if summary and should_replace_feed_summary(
                snapshot['feed_summary'], summary, title
            ):
                updates.append((publication_id, summary))
        if updates:
            with platform_session_scope() as scoped:
                by_id = {
                    publication.id: publication
                    for publication in scoped.query(RegulatoryPublication)
                    .filter(RegulatoryPublication.id.in_([item[0] for item in updates]))
                    .all()
                }
                for publication_id, summary in updates:
                    record = by_id.get(publication_id)
                    if record is not None:
                        record.feed_summary = summary
                scoped.commit()

    from app.services.regulatory_reference_policy_service import (
        sync_regulatory_reference_policies_releasing,
    )

    reference_policy_sync_count = await sync_regulatory_reference_policies_releasing(
        selected_sources,
        settings,
        org_id=org_id,
    )

    return RegulatoryIngestResponse(
        status="completed",
        sources=sorted(selected_sources),
        fetched_count=len(scraped.publications),
        created_count=created_count,
        duplicate_count=duplicates,
        embedded_count=embedded_count,
        reference_policy_sync_count=reference_policy_sync_count,
        emitted_event_count=0,
        errors=scraped.errors,
    )


async def collect_regulatory_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    sources: Iterable[str] | None = None,
    *,
    known_urls_by_source: dict[str, set[str]] | None = None,
) -> IngestResult:
    """Collect publications from independent regulator sources without mutating state."""
    selected_sources = normalized_sources(sources)
    publications: list[ScrapedPublication] = []
    errors: list[str] = []
    known = known_urls_by_source or {}

    scrapers: dict[str, Any] = {
        SEC_SOURCE: scrape_sec_publications,
        FINRA_SOURCE: scrape_finra_publications,
        CFTC_SOURCE: scrape_cftc_publications,
        NFA_SOURCE: scrape_nfa_publications,
    }
    for source in sorted(selected_sources):
        before = len(publications)
        try:
            scraper = scrapers[source]
            if source == FINRA_SOURCE:
                publications.extend(
                    await scrape_finra_publications(
                        client,
                        settings,
                        known_urls=known.get(source, set()),
                    )
                )
            elif source == SEC_SOURCE:
                publications.extend(
                    await scrape_sec_publications(
                        client,
                        settings,
                        known_urls=known.get(source, set()),
                    )
                )
            elif source == CFTC_SOURCE:
                publications.extend(
                    await scrape_cftc_publications(
                        client,
                        settings,
                        known_urls=known.get(source, set()),
                    )
                )
            else:
                publications.extend(await scraper(client, settings))
            logger.info(
                "regulatory_source_scrape_completed",
                source=source,
                fetched_count=len(publications) - before,
            )
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("regulatory_source_scrape_failed",
                           source=source, error=str(exc))
            errors.append(f"{source}: {type(exc).__name__}")

    return IngestResult(
        publications=deduplicate_scraped_publications(publications),
        errors=errors,
    )


async def scrape_sec_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    known_urls: set[str] | None = None,
) -> list[ScrapedPublication]:
    """Scrape SEC newsroom HTML and RSS. Issuer EDGAR filings are not ingested."""
    publications: list[ScrapedPublication] = []
    known = known_urls or set()
    newsroom_count = 0

    # Official SEC newsroom HTML hub, then press/speech RSS backups.
    newsroom_urls = settings.sec_newsroom_url_list
    if newsroom_urls:
        newsroom_pubs = await scrape_feed_or_listing_publications(
            client,
            settings,
            SEC_SOURCE,
            newsroom_urls,
            article_delay_seconds=1.0,
            article_link_prefixes=SEC_NEWSROOM_ARTICLE_PATH_PREFIXES,
            section_link_prefixes=SEC_NEWSROOM_SECTION_PATH_PREFIXES,
            limiter=get_sec_newsroom_limiter(settings),
            fail_soft=True,
            known_urls=known,
            full_text_attempts=2,
            pause_full_text_on_rate_limit=True,
        )
        publications.extend(newsroom_pubs)
        newsroom_count = len(newsroom_pubs)
    else:
        logger.warning("sec_newsroom_urls_empty")

    logger.info(
        "sec_publications_scraped",
        newsroom_count=newsroom_count,
        newsroom_feeds=len(newsroom_urls),
    )
    return publications


async def scrape_finra_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    known_urls: set[str] | None = None,
) -> list[ScrapedPublication]:
    # Official media-center hub plus RSS. FINRA HTML rate-limits aggressively (HTTP 429);
    # fail_soft keeps sync moving when the hub or a feed is blocked.
    return await scrape_feed_or_listing_publications(
        client,
        settings,
        FINRA_SOURCE,
        settings.finra_rss_url_list,
        article_delay_seconds=0.0,
        article_link_prefixes=FINRA_ARTICLE_PATH_PREFIXES,
        section_link_prefixes=FINRA_SECTION_PATH_PREFIXES,
        limiter=get_finra_limiter(settings),
        fail_soft=True,
        known_urls=known_urls or set(),
        full_text_attempts=2,
        pause_full_text_on_rate_limit=True,
    )


async def scrape_cftc_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    known_urls: set[str] | None = None,
) -> list[ScrapedPublication]:
    # Official Press Room hub (follows into PressReleases) plus press-release RSS.
    return await scrape_feed_or_listing_publications(
        client,
        settings,
        CFTC_SOURCE,
        settings.cftc_rss_url_list,
        article_link_prefixes=CFTC_ARTICLE_PATH_PREFIXES,
        section_link_prefixes=CFTC_SECTION_PATH_PREFIXES,
        fail_soft=True,
        known_urls=known_urls or set(),
        full_text_attempts=2,
        pause_full_text_on_rate_limit=True,
        article_path_pattern=CFTC_ARTICLE_PATH_PATTERN,
    )


async def scrape_nfa_publications(
    client: httpx.AsyncClient,
    settings: Settings,
) -> list[ScrapedPublication]:
    return await scrape_feed_or_listing_publications(
        client,
        settings,
        NFA_SOURCE,
        settings.nfa_rss_url_list,
        article_link_prefixes=(),
        section_link_prefixes=(),
        fail_soft=True,
    )


async def scrape_feed_or_listing_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    source: str,
    feed_urls: Sequence[str],
    article_delay_seconds: float = 0.0,
    article_link_prefixes: Sequence[str] = (),
    section_link_prefixes: Sequence[str] = (),
    *,
    limiter: AsyncRateLimiter | None = None,
    fail_soft: bool = False,
    known_urls: set[str] | None = None,
    full_text_attempts: int | None = None,
    pause_full_text_on_rate_limit: bool = False,
    article_path_pattern: re.Pattern[str] | None = None,
) -> list[ScrapedPublication]:
    """Parse RSS feeds or HTML listings; optionally load linked article text."""
    publications: list[ScrapedPublication] = []
    known = known_urls or set()
    article_attempts = (
        full_text_attempts
        if full_text_attempts is not None
        else settings.regulatory_http_max_attempts
    )
    listing_attempts = (
        FAIL_SOFT_LISTING_ATTEMPTS
        if fail_soft
        else settings.regulatory_http_max_attempts
    )
    # Prefer RSS summaries when present. Fetch HTML only when configured, or when
    # the feed entry has no summary (common for CFTC). Pause after 429/503.
    full_text_paused = False
    for feed_url in feed_urls:
        try:
            response, resolved_feed_url = await fetch_feed_or_listing_response(
                client,
                feed_url,
                limiter=limiter,
                attempts=listing_attempts,
            )
            parsed = feedparser.parse(response.content)
            if parsed.entries:
                for entry in list(parsed.entries)[: settings.regulatory_max_items_per_feed]:
                    url = str(entry.get("link") or "").strip()
                    title = clean_feed_title(str(entry.get("title") or ""))
                    if not url or not title:
                        continue
                    if is_issuer_filing_publication(url=url, title=title):
                        continue
                    if url in known:
                        continue
                    summary = entry_summary(entry)
                    raw_text = summary
                    from app.services.publication_summary_service import (
                        publication_text_is_title_only,
                    )

                    want_full_text = (
                        not full_text_paused
                        and (
                            settings.regulatory_fetch_full_text
                            or not (raw_text or '').strip()
                            or publication_text_is_title_only(raw_text, title)
                        )
                    )
                    if want_full_text:
                        fetched, rate_limited = await fetch_html_text_with_status(
                            client,
                            url,
                            limiter=limiter,
                            attempts=article_attempts,
                        )
                        if fetched:
                            raw_text = fetched
                        elif rate_limited and pause_full_text_on_rate_limit:
                            full_text_paused = True
                            logger.warning(
                                "publication_full_text_paused_after_rate_limit",
                                source=source,
                                url=url,
                            )
                    if not raw_text:
                        raw_text = title
                    publications.append(
                        ScrapedPublication(
                            source=source,
                            title=title[:500],
                            url=url,
                            published_at=publication_date_from_entry(entry),
                            raw_text=raw_text,
                        )
                    )
                    if article_delay_seconds:
                        await asyncio.sleep(article_delay_seconds)
                continue

            if response.text.strip():
                html_publications = await scrape_html_publications(
                    client=client,
                    settings=settings,
                    source=source,
                    page_url=resolved_feed_url,
                    page_html=response.text,
                    article_delay_seconds=article_delay_seconds,
                    article_link_prefixes=article_link_prefixes,
                    section_link_prefixes=section_link_prefixes,
                    limiter=limiter,
                    known_urls=known,
                    full_text_attempts=article_attempts,
                    pause_full_text_on_rate_limit=pause_full_text_on_rate_limit,
                    article_path_pattern=article_path_pattern,
                )
                if html_publications:
                    publications.extend(html_publications)
                    continue
                logger.info(
                    "regulatory_feed_listing_empty",
                    source=source,
                    feed_url=feed_url,
                )
                if fail_soft:
                    continue
            raise ValueError(f"Unable to parse {source} feed or listing {feed_url}")
        except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
            log = logger.info if fail_soft else logger.warning
            log(
                "regulatory_feed_scrape_failed",
                source=source,
                feed_url=feed_url,
                error_type=type(exc).__name__,
                error=str(exc) or repr(exc),
            )
            if not fail_soft:
                raise
            continue
    return publications


def feed_url_candidates(feed_url: str) -> list[str]:
    """Return alternate scheme candidates for known flaky feed hosts."""
    parsed = urlparse(feed_url)
    candidates = [feed_url]
    host = (parsed.netloc or "").lower()
    if host == "feeds.finra.org" and parsed.scheme in {"http", "https"}:
        alternate_scheme = "http" if parsed.scheme == "https" else "https"
        alternate = parsed._replace(scheme=alternate_scheme).geturl()
        if alternate not in candidates:
            candidates.append(alternate)
    return candidates


async def fetch_feed_or_listing_response(
    client: httpx.AsyncClient,
    feed_url: str,
    *,
    limiter: AsyncRateLimiter | None = None,
    attempts: int = 3,
) -> tuple[httpx.Response, str]:
    """Fetch a feed URL, falling back across scheme candidates when needed."""
    last_error: Exception | None = None
    for candidate in feed_url_candidates(feed_url):
        try:
            response = await get_with_retries(
                client,
                candidate,
                limiter=limiter,
                attempts=attempts,
            )
            if candidate != feed_url:
                logger.info(
                    "regulatory_feed_url_fallback_used",
                    requested_url=feed_url,
                    resolved_url=candidate,
                )
            return response, candidate
        except (httpx.HTTPError, RuntimeError) as exc:
            last_error = exc
            logger.info(
                "regulatory_feed_url_candidate_failed",
                requested_url=feed_url,
                candidate_url=candidate,
                error_type=type(exc).__name__,
                error=str(exc) or repr(exc),
            )
    assert last_error is not None
    raise last_error


def clean_feed_title(value: str) -> str:
    """Strip HTML wrappers that some FINRA RSS titles include."""
    text = value.strip()
    if not text:
        return ""
    if "<" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return " ".join(text.split())


async def scrape_html_publications(
    client: httpx.AsyncClient,
    settings: Settings,
    source: str,
    page_url: str,
    page_html: str,
    article_delay_seconds: float = 0.0,
    article_link_prefixes: Sequence[str] = (),
    section_link_prefixes: Sequence[str] = (),
    *,
    limiter: AsyncRateLimiter | None = None,
    known_urls: set[str] | None = None,
    full_text_attempts: int | None = None,
    pause_full_text_on_rate_limit: bool = False,
    article_path_pattern: re.Pattern[str] | None = None,
) -> list[ScrapedPublication]:
    known = known_urls or set()
    article_attempts = (
        full_text_attempts
        if full_text_attempts is not None
        else settings.regulatory_http_max_attempts
    )
    listing_attempts = min(FAIL_SOFT_LISTING_ATTEMPTS, settings.regulatory_http_max_attempts)
    publication_candidates = extract_html_publication_candidates(
        page_html,
        page_url,
        article_link_prefixes,
        article_path_pattern=article_path_pattern,
    )
    if not publication_candidates and section_link_prefixes:
        discovered_url = discover_section_url(
            page_html,
            page_url,
            section_link_prefixes,
        )
        if discovered_url and discovered_url != page_url:
            response = await get_with_retries(
                client,
                discovered_url,
                limiter=limiter,
                attempts=listing_attempts,
            )
            publication_candidates = extract_html_publication_candidates(
                response.text,
                discovered_url,
                article_link_prefixes,
                article_path_pattern=article_path_pattern,
            )

    publications: list[ScrapedPublication] = []
    full_text_paused = not settings.regulatory_fetch_full_text
    for index, (title, url, published_at) in enumerate(
        publication_candidates[: settings.regulatory_max_items_per_feed]
    ):
        if url in known:
            continue
        if is_issuer_filing_publication(url=url, title=title):
            continue
        raw_text: str | None = None
        if not full_text_paused:
            fetched, rate_limited = await fetch_html_text_with_status(
                client,
                url,
                limiter=limiter,
                attempts=article_attempts,
            )
            raw_text = fetched
            if rate_limited and pause_full_text_on_rate_limit:
                full_text_paused = True
                logger.warning(
                    "publication_full_text_paused_after_rate_limit",
                    source=source,
                    url=url,
                )
        if not raw_text:
            raw_text = title
        publications.append(
            ScrapedPublication(
                source=source,
                title=title[:500],
                url=url,
                published_at=published_at,
                raw_text=raw_text,
            )
        )
        if article_delay_seconds and index + 1 < len(publication_candidates):
            await asyncio.sleep(article_delay_seconds)
    return publications


def retry_after_seconds(response: httpx.Response, attempt: int) -> float:
    """Compute backoff for 429/5xx responses, honoring Retry-After when present."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after.strip()), 1.0), 90.0)
        except ValueError:
            pass
    if response.status_code == 429:
        return min(5.0 * (2**attempt), 60.0)
    # SEC newsroom often returns 503 under load; keep short backoff so upload
    # workflows fail soft instead of spending minutes retrying every article.
    if response.status_code == 503:
        return min(1.0 * (2**attempt), 4.0)
    return float(2**attempt)


async def get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    limiter: AsyncRateLimiter | None = None,
    attempts: int = 3,
) -> httpx.Response:
    """Fetch an external source with bounded retry behavior for transient failures."""
    for attempt in range(attempts):
        if limiter is not None:
            await limiter.wait()
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    delay = retry_after_seconds(response, attempt)
                    logger.info(
                        "regulatory_http_retry",
                        url=url,
                        status_code=response.status_code,
                        attempt=attempt + 1,
                        delay_seconds=round(delay, 2),
                    )
                    if limiter is not None and response.status_code == 429:
                        await limiter.penalize(delay)
                    await asyncio.sleep(delay)
                    continue
            response.raise_for_status()
            return response
        except httpx.RequestError:
            if attempt + 1 == attempts:
                raise
            await asyncio.sleep(2**attempt)
    raise RuntimeError(f"Unable to fetch {url}")


async def fetch_html_text(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: AsyncRateLimiter | None = None,
    attempts: int = 3,
) -> str | None:
    text, _rate_limited = await fetch_html_text_with_status(
        client,
        url,
        limiter=limiter,
        attempts=attempts,
    )
    return text


async def fetch_html_text_with_status(
    client: httpx.AsyncClient,
    url: str,
    *,
    limiter: AsyncRateLimiter | None = None,
    attempts: int = 3,
) -> tuple[str | None, bool]:
    """Fetch article HTML text.

    Second value is True when the final failure was an upstream throttle/outage
    (HTTP 429 or 503) so callers can pause remaining full-text fetches.
    """
    try:
        headers = build_sec_request_headers()
        response = await get_with_retries(
            client,
            url,
            headers=headers,
            limiter=limiter,
            attempts=attempts,
        )
        return extract_html_text(response.text), False
    except httpx.HTTPStatusError as exc:
        rate_limited = exc.response.status_code in {429, 503}
        logger.warning(
            "publication_full_text_fetch_failed",
            url=url,
            status_code=exc.response.status_code,
            error=str(exc) or repr(exc),
        )
        return None, rate_limited
    except (httpx.HTTPError, RuntimeError) as exc:
        logger.warning(
            "publication_full_text_fetch_failed",
            url=url,
            error_type=type(exc).__name__,
            error=str(exc) or repr(exc),
        )
        return None, False

def extract_html_text(html: str) -> str | None:
    """Extract readable article content while omitting scripts and page chrome."""
    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "noscript", "svg", "nav", "header", "footer", "aside"]):
        element.decompose()
    content = soup.find("article") or soup.find("main") or soup.body or soup
    text = " ".join(content.stripped_strings)
    return text[:MAX_PUBLICATION_TEXT_LENGTH] or None


def persist_new_publications(
    db: Session,
    publications: Sequence[ScrapedPublication],
) -> tuple[list[RegulatoryPublication], int]:
    """Persist fresh publications and safely tolerate duplicate source URLs."""
    if not publications:
        return [], 0

    keys = [(publication.source, publication.url)
            for publication in publications]
    existing_by_key = {
        (record.source, record.url): record
        for record in db.query(RegulatoryPublication)
        .filter(tuple_(RegulatoryPublication.source, RegulatoryPublication.url).in_(keys))
        .all()
    }
    from app.services.publication_summary_service import publication_text_is_thin

    created: list[RegulatoryPublication] = []
    duplicates = 0
    try:
        for publication in publications:
            if is_issuer_filing_publication(url=publication.url, title=publication.title):
                logger.info(
                    "issuer_filing_skipped",
                    url=(publication.url or "")[:180],
                    title=(publication.title or "")[:120],
                )
                continue
            key = (publication.source, publication.url)
            existing = existing_by_key.get(key)
            if existing is not None:
                incoming = (publication.raw_text or '').strip()
                current = (existing.raw_text or '').strip()
                if incoming and (
                    not current or publication_text_is_thin(current, existing.title)
                ):
                    if len(incoming) > len(current):
                        existing.raw_text = incoming
                        existing.feed_summary = None
                duplicates += 1
                continue
            record = RegulatoryPublication(
                id=str(uuid4()),
                source=publication.source,
                title=publication.title,
                url=publication.url,
                published_at=publication.published_at,
                raw_text=publication.raw_text,
                processed=False,
                embedding_chunk_ids=[],
            )
            try:
                with db.begin_nested():
                    db.add(record)
                    db.flush()
            except IntegrityError:
                duplicates += 1
                continue
            existing_by_key[key] = record
            created.append(record)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "regulatory_publication_persistence_failed", error_type=type(exc).__name__)
        raise
    return created, duplicates


def list_publications_missing_embeddings(
    db: Session,
    sources: Iterable[str],
) -> list[RegulatoryPublication]:
    """Return scraped publications that still need chunk embeddings."""
    selected = normalized_sources(sources)
    if not selected:
        return []
    candidates = (
        db.query(RegulatoryPublication)
        .filter(RegulatoryPublication.source.in_(selected))
        .order_by(RegulatoryPublication.created_at.desc())
        .limit(200)
        .all()
    )
    return [
        publication
        for publication in candidates
        if publication_has_scrape_content(publication)
        and not publication_has_embeddings(publication)
    ]


async def embed_created_publications(
    db: Session,
    publications: Sequence[RegulatoryPublication],
    settings: Settings,
) -> int:
    """Chunk and embed newly scraped publications for semantic retrieval."""
    if not publications or not settings.openai_api_key:
        return 0

    from app.services.document_ingestion import chunk_publication_text, generate_embeddings

    snapshots: list[dict[str, Any]] = []
    for publication in publications:
        if publication.embedding_chunk_ids:
            continue
        snapshots.append(
            {
                "id": publication.id,
                "title": publication.title,
                "raw_text": publication.raw_text,
                "source": publication.source,
                "url": publication.url,
            }
        )

    prepared: list[tuple[dict[str, Any], list[Any], list[list[float]]]] = []
    for snapshot in snapshots:
        chunks = chunk_publication_text(snapshot["title"], snapshot["raw_text"])
        if not chunks:
            continue
        embeddings = await generate_embeddings(chunks, settings, org_id=None)
        if not embeddings:
            continue
        prepared.append((snapshot, chunks, embeddings))

    return persist_prepared_publication_embeddings(db, prepared, settings)


def persist_prepared_publication_embeddings(
    db: Session,
    prepared: Sequence[tuple[dict[str, Any], list[Any], list[list[float]]]],
    settings: Settings,
) -> int:
    """Write precomputed publication embeddings (call after embeddings finish offline).

    Publication rows and their embeddings are platform-wide. Force ``public``
    so a tenant-bound session cannot insert ``org_id='global'`` chunks into
    the org's cloned ``document_chunks`` table.
    """
    if not prepared:
        return 0

    from app.db.session import use_shared_search_path

    use_shared_search_path(db)

    embedded = 0
    for snapshot, chunks, embeddings in prepared:
        publication = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.id == snapshot["id"])
            .one_or_none()
        )
        if publication is None or publication.embedding_chunk_ids:
            continue
        chunk_ids: list[str] = []
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            chunk_id = str(uuid4())
            chunk_ids.append(chunk_id)
            db.add(
                DocumentChunk(
                    id=chunk_id,
                    org_id="global",
                    publication_id=publication.id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    document_type=REGULATORY_PUBLICATION_CHUNK_TYPE,
                    embedding=embedding,
                    metadata_json={
                        **chunk.metadata,
                        "source": snapshot["source"],
                        "publication_url": snapshot["url"],
                        "policy_type": REGULATORY_PUBLICATION_CHUNK_TYPE,
                        "embedding_model": settings.openai_embedding_model,
                        "embedded": True,
                    },
                )
            )
        publication.embedding_chunk_ids = chunk_ids
        embedded += 1
    if embedded:
        try:
            db.commit()
        except SQLAlchemyError as exc:
            db.rollback()
            logger.exception(
                "publication_embedding_persistence_failed",
                error_type=type(exc).__name__,
            )
            raise
    return embedded


def ordered_feed_sources(sources: Iterable[str] | None) -> list[str]:
    """Stable agency order: SEC, FINRA, CFTC, then any remaining sources."""
    wanted = {str(source).strip().upper() for source in (sources or ()) if str(source).strip()}
    ordered = [source for source in PREFERRED_SOURCE_ORDER if source in wanted]
    extras = sorted(wanted.difference(ordered))
    return ordered + extras


def interleave_source_buckets(
    buckets: Mapping[str, Sequence[Any]],
    *,
    sources: Sequence[str],
    limit: int,
) -> list[Any]:
    """Round-robin newest-first buckets so each agency appears near the top."""
    indexes = {source: 0 for source in sources}
    selected: list[Any] = []
    seen: set[str] = set()

    def _identity(item: Any) -> str:
        item_id = getattr(item, 'id', None)
        if item_id:
            return str(item_id)
        if isinstance(item, dict):
            return str(item.get('id') or item.get('url') or id(item))
        return str(getattr(item, 'url', None) or id(item))

    progressed = True
    while len(selected) < limit and progressed:
        progressed = False
        for source in sources:
            if len(selected) >= limit:
                break
            bucket = buckets.get(source) or ()
            cursor = indexes[source]
            while cursor < len(bucket):
                item = bucket[cursor]
                cursor += 1
                indexes[source] = cursor
                key = _identity(item)
                if key in seen:
                    continue
                seen.add(key)
                selected.append(item)
                progressed = True
                break
    return selected


def _load_diverse_publication_rows(
    db: Session,
    *,
    sources: Sequence[str],
    limit: int,
) -> list[RegulatoryPublication]:
    """Load newest rows per agency, then interleave so SEC/FINRA/CFTC are all visible."""
    if limit <= 0 or not sources:
        return []
    if len(sources) == 1:
        query = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.source == sources[0])
            .filter(_exclude_issuer_filings_clause())
        )
        rows = _load_publication_rows(db, query, limit)
        return [
            publication
            for publication in rows
            if not is_issuer_filing_publication(url=publication.url, title=publication.title)
        ][:limit]

    per_source = max(1, (limit + len(sources) - 1) // len(sources))
    fetch_limit = per_source + 12
    buckets: dict[str, list[RegulatoryPublication]] = {}
    for source in sources:
        query = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.source == source)
            .filter(_exclude_issuer_filings_clause())
        )
        rows = _load_publication_rows(db, query, fetch_limit)
        buckets[source] = [
            publication
            for publication in rows
            if not is_issuer_filing_publication(url=publication.url, title=publication.title)
        ]
    return interleave_source_buckets(buckets, sources=sources, limit=limit)


def newest_publication_for_source(
    db: Session,
    source: str,
    *,
    exclude_ids: Iterable[str] | None = None,
) -> RegulatoryPublication | None:
    """Newest non-filing publication for one regulator, used to fill empty newsletter sections."""
    blocked = {item for item in (exclude_ids or ()) if item}
    query = (
        db.query(RegulatoryPublication)
        .filter(RegulatoryPublication.source == source)
        .filter(_exclude_issuer_filings_clause())
    )
    rows = _load_publication_rows(db, query, 12)
    for publication in rows:
        if publication.id and publication.id in blocked:
            continue
        if is_issuer_filing_publication(url=publication.url, title=publication.title):
            continue
        return publication
    return None


def list_publications_for_user(
    db: Session | None,
    org_id: str,
    is_internal_user: bool,
    limit: int,
) -> list[RegulatoryPublicationResponse]:
    """Return source-scoped publications for a customer, or all records for staff."""
    if db is None:
        return []

    from app.db.session import use_shared_search_path

    use_shared_search_path(db)

    if is_internal_user:
        sources = ordered_feed_sources(ALL_SOURCES)
    else:
        organization = db.query(Organization).filter(
            Organization.auth_org_id == org_id).one_or_none()
        if organization is None:
            return []
        source_scope = organization_source_scope(organization)
        if not source_scope:
            return []
        sources = ordered_feed_sources(source_scope)
        if not sources:
            return []

    publications = _load_diverse_publication_rows(db, sources=sources, limit=limit)
    matched_ids = _matched_publication_ids(
        db,
        org_id=org_id,
        is_internal_user=is_internal_user,
        publication_ids=[publication.id for publication in publications],
    )
    return [
        _publication_response(
            publication,
            matched=publication.id in matched_ids,
        )
        for publication in publications
    ]


def list_shared_feed_publications(
    *,
    sources: Sequence[str] | None = None,
    limit: int = 50,
) -> list[RegulatoryPublicationResponse]:
    """Return the shared government feed used by the public newsletter page."""
    from app.db.session import platform_session_scope, use_shared_search_path

    scoped = ordered_feed_sources(sources) if sources else ordered_feed_sources(ALL_SOURCES)
    with platform_session_scope(commit=False) as db:
        use_shared_search_path(db)
        publications = _load_diverse_publication_rows(db, sources=scoped, limit=limit)
        return [
            _publication_response(publication, matched=False)
            for publication in publications
        ]


def _matched_publication_ids(
    db: Session,
    *,
    org_id: str,
    is_internal_user: bool,
    publication_ids: Sequence[str],
) -> set[str]:
    """Return publication IDs directly linked to a gap analysis for this viewer."""
    if not publication_ids:
        return set()
    query = (
        db.query(GapAnalysis.publication_id)
        .filter(GapAnalysis.publication_id.in_(list(publication_ids)))
        .filter(GapAnalysis.publication_id.is_not(None))
    )
    if not is_internal_user:
        query = query.filter(GapAnalysis.org_id == org_id)
    return {row[0] for row in query.distinct().all() if row[0]}


def _load_publication_rows(
    db: Session,
    query: Any,
    limit: int,
) -> list[RegulatoryPublication]:
    """Load feed rows, skipping ``feed_summary`` if the column is not migrated yet."""

    def fetch(active_query: Any) -> list[RegulatoryPublication]:
        return (
            active_query.order_by(
                RegulatoryPublication.published_at.desc().nullslast(),
                RegulatoryPublication.created_at.desc(),
            )
            .limit(limit)
            .all()
        )

    try:
        return fetch(query)
    except ProgrammingError:
        rollback = getattr(db, 'rollback', None)
        if callable(rollback):
            rollback()
        logger.warning('regulatory_publications_feed_summary_column_missing')
        return fetch(query.options(defer(RegulatoryPublication.feed_summary)))


def _publication_response(
    publication: RegulatoryPublication,
    *,
    matched: bool = False,
) -> RegulatoryPublicationResponse:
    status = publication_feed_status(publication, matched=matched)
    created_at = publication.created_at or datetime.now(UTC)
    return RegulatoryPublicationResponse(
        id=publication.id or '',
        source=publication.source or '',
        title=publication.title or '',
        url=publication.url or '',
        published_at=publication.published_at,
        processed=status in {'ingested', 'matched'},
        status=status,
        summary=publication_summary(publication),
        created_at=created_at,
    )


def publication_has_scrape_content(publication: RegulatoryPublication) -> bool:
    """True when the scraper has persisted full regulation text."""
    return bool((publication.raw_text or '').strip())


def publication_has_embeddings(publication: RegulatoryPublication) -> bool:
    """True when publication chunks have been embedded into the library."""
    return bool(publication.embedding_chunk_ids or [])


def publication_feed_status(
    publication: RegulatoryPublication,
    *,
    matched: bool = False,
) -> str:
    """Return feed status for the Regulatory Feed table.

    - matched: publication is directly linked to an org gap analysis
    - ingested: scrape finished and content is saved (default feed state)
    """
    _ = publication
    if matched:
        return 'matched'
    return 'ingested'


def publication_summary(publication: RegulatoryPublication) -> str | None:
    """Return the LLM feed summary, or a longer body excerpt if the stored text is clipped."""
    stored = _loaded_feed_summary(publication)
    from app.services.publication_summary_service import (
        fallback_publication_excerpt,
        feed_summary_looks_clipped,
        publication_text_is_thin,
    )

    excerpt = fallback_publication_excerpt(publication.raw_text)
    title = publication.title or ''
    # Prefer a complete LLM brief. Clipped stored text is only a last resort
    # when the raw excerpt is even shorter (thin RSS bodies).
    if stored and not publication_text_is_thin(stored, title) and not feed_summary_looks_clipped(stored):
        return stored
    if excerpt and (
        not stored
        or publication_text_is_thin(stored, title)
        or (feed_summary_looks_clipped(stored) and len(excerpt) > len(stored))
    ):
        return excerpt
    return stored or excerpt


def _loaded_feed_summary(publication: RegulatoryPublication) -> str:
    """Read ``feed_summary`` only when the column is present and already loaded."""
    try:
        from sqlalchemy.orm.attributes import instance_state

        state = instance_state(publication)
    except Exception:
        return (getattr(publication, 'feed_summary', None) or '').strip()
    if 'feed_summary' in state.unloaded:
        return ''
    return (publication.feed_summary or '').strip()


def publication_matches_organization(source: str, organization: Organization) -> bool:
    return source.upper() in organization_source_scope(organization)


def organization_source_scope(organization: Organization) -> set[str]:
    scope = organization.regulatory_scope
    if isinstance(scope, list):
        sources = scope
    elif isinstance(scope, dict):
        sources = scope.get('sources', [])
    else:
        sources = []
    if not isinstance(sources, list):
        return set()
    return {str(source).strip().upper() for source in sources if str(source).strip()}


def normalized_sources(sources: Iterable[str] | None) -> set[str]:
    if sources is None:
        return set(ALL_SOURCES)
    return {str(source).strip().upper() for source in sources if str(source).strip().upper() in ALL_SOURCES}


def deduplicate_scraped_publications(
    publications: Iterable[ScrapedPublication],
) -> list[ScrapedPublication]:
    unique: dict[tuple[str, str], ScrapedPublication] = {}
    for publication in publications:
        if is_issuer_filing_publication(url=publication.url, title=publication.title):
            continue
        unique.setdefault((publication.source, publication.url), publication)
    return list(unique.values())


def publication_date_from_entry(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)
    return parse_iso_date(entry.get("published") or entry.get("updated"))


def parse_iso_date(value: Any) -> datetime | None:
    if not value:
        return None
    raw_value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def entry_summary(entry: Any) -> str | None:
    summary = entry.get("summary") or entry.get("description")
    return extract_html_text(str(summary)) if summary else None


def value_at(values: Any, index: int) -> str | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    value = values[index]
    return str(value).strip() if value is not None else None


def discover_section_url(
    page_html: str,
    base_url: str,
    section_link_prefixes: Sequence[str],
) -> str | None:
    soup = BeautifulSoup(page_html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]).strip())
        if path_matches_prefixes(href, section_link_prefixes):
            return href
    return None


def extract_html_publication_candidates(
    page_html: str,
    base_url: str,
    article_link_prefixes: Sequence[str],
    *,
    article_path_pattern: re.Pattern[str] | None = None,
) -> list[tuple[str, str, datetime | None]]:
    soup = BeautifulSoup(page_html, "html.parser")
    seen_urls: set[str] = set()
    candidates: list[tuple[str, str, datetime | None]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]).strip())
        path = urlparse(href).path
        if article_path_pattern is not None:
            if not article_path_pattern.search(path):
                continue
        elif not path_matches_prefixes(href, article_link_prefixes):
            continue
        title = normalize_text(anchor.get_text(" ", strip=True))
        if not title or href in seen_urls:
            continue
        seen_urls.add(href)
        candidates.append((title, href, publication_date_from_context(anchor)))
    return candidates


def publication_date_from_context(anchor: Any) -> datetime | None:
    current = anchor
    for _ in range(4):
        if current is None:
            break
        text = normalize_text(
            getattr(current, "get_text", lambda *args, **kwargs: "")(" ", strip=True))
        if text:
            parsed = parse_date_from_text(text)
            if parsed is not None:
                return parsed
        current = getattr(current, "parent", None)
    return None


def path_matches_prefixes(url: str, prefixes: Sequence[str]) -> bool:
    if not prefixes:
        return False
    path = urlparse(url).path
    return any(path.startswith(prefix) for prefix in prefixes)


def parse_date_from_text(text: str) -> datetime | None:
    candidates = (
        r"\b(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\s+\d{1,2},\s+\d{4}\b",
        r"\b\d{1,2}/\d{1,2}/\d{4}\b",
        r"\b\d{1,2}\s+(?:Jan|January|Feb|February|Mar|March|Apr|April|May|Jun|June|Jul|July|Aug|August|Sep|Sept|September|Oct|October|Nov|November|Dec|December)\s+\d{4}\b",
    )
    for pattern in candidates:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        date_text = match.group(0)
        for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
            try:
                parsed = datetime.strptime(date_text, fmt)
                return parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return None


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
