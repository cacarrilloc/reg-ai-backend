from contextlib import nullcontext
from types import SimpleNamespace

import httpx
import pytest
from ai_fakes import FakeLLM

from app.core.config import Settings
from app.core.inngest import publication_created_handler
from app.models.core import Organization, RegulatoryPublication
from app.services.regulatory_monitoring_service import (
    ScrapedPublication,
    build_sec_request_headers,
    clean_feed_title,
    extract_html_text,
    feed_url_candidates,
    get_with_retries,
    is_issuer_filing_publication,
    is_sec_edgar_filing_url,
    persist_new_publications,
    publication_matches_organization,
    publication_summary,
    regulatory_sources,
    retry_after_seconds,
    scrape_cftc_publications,
    scrape_finra_publications,
    scrape_sec_publications,
)


def test_regulatory_sources_include_plan_sources() -> None:
    sources = {source["source"]: source for source in regulatory_sources()}

    assert {"SEC", "FINRA", "CFTC", "NFA"}.issubset(sources)
    assert "newsroom" in sources["SEC"]["mode"]
    assert "media-center" in sources["FINRA"]["mode"]
    assert "PressRoom" in sources["CFTC"]["mode"]


def test_default_regulatory_urls_include_official_hub_pages() -> None:
    settings = Settings(
        SEC_NEWSROOM_URLS=Settings.model_fields["sec_newsroom_urls"].default,
        FINRA_RSS_URLS=Settings.model_fields["finra_rss_urls"].default,
        CFTC_RSS_URLS=Settings.model_fields["cftc_rss_urls"].default,
    )
    assert "https://www.sec.gov/newsroom" in settings.sec_newsroom_url_list
    assert "https://www.sec.gov/news/pressreleases.rss" in settings.sec_newsroom_url_list
    assert "https://www.finra.org/media-center" in settings.finra_rss_url_list
    assert "http://feeds.finra.org/FINRANotices" in settings.finra_rss_url_list
    assert "https://www.cftc.gov/PressRoom/index.htm" in settings.cftc_rss_url_list
    assert "https://www.cftc.gov/RSS/RSSGP/rssgp.xml" in settings.cftc_rss_url_list


def test_sec_request_headers_include_user_agent() -> None:
    headers = build_sec_request_headers()

    assert headers["User-Agent"].startswith("Reg-AI")
    assert "application/json" in headers["Accept"]


def test_is_sec_edgar_filing_url_detects_archive_paths() -> None:
    assert is_sec_edgar_filing_url(
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-20260627.htm"
    )
    assert is_sec_edgar_filing_url(
        "https://www.sec.gov/Archives/edgar/data/320193/000195004726007959/xsl144X01/primary_doc.xml"
    )
    assert not is_sec_edgar_filing_url(
        "https://www.sec.gov/newsroom/press-releases/2026-72-example"
    )


def test_issuer_filings_are_excluded_from_the_regulatory_feed() -> None:
    assert is_issuer_filing_publication(
        title="Apple Inc.: 144",
        url="https://www.sec.gov/Archives/edgar/data/320193/000195004726007959/xsl144X01/primary_doc.xml",
    )
    assert is_issuer_filing_publication(title="Apple Inc.: 144", url="https://www.sec.gov/newsroom/unrelated")
    assert is_issuer_filing_publication(title="Form 144", url="https://www.sec.gov/newsroom/unrelated")
    assert not is_issuer_filing_publication(
        title="CFTC Releases Advisory on Self-Certification of Incentive Programs for Prediction Markets",
        url="https://www.cftc.gov/PressRoom/PressReleases/example",
    )
    assert not is_issuer_filing_publication(
        title="SEC Adopts Amendments to Rule 144",
        url="https://www.sec.gov/newsroom/press-releases/2026-72-example",
    )


def test_persist_skips_apple_form_144_filings() -> None:
    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return []

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, *_args):
            return Query()

        def begin_nested(self):
            return nullcontext()

        def add(self, value: object) -> None:
            self.added.append(value)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    created, duplicates = persist_new_publications(
        RecordingSession(),  # type: ignore[arg-type]
        [
            ScrapedPublication(
                "SEC",
                "Apple Inc.: 144",
                "https://www.sec.gov/Archives/edgar/data/320193/0001/xsl144X01/primary_doc.xml",
                None,
                "Form 144 Filer Information UNITED STATES",
            ),
            ScrapedPublication(
                "CFTC",
                "CFTC Releases Advisory on Self-Certification of Incentive Programs",
                "https://www.cftc.gov/PressRoom/PressReleases/example",
                None,
                "Advisory text",
            ),
        ],
    )
    assert duplicates == 0
    assert len(created) == 1
    assert created[0].source == "CFTC"


def test_list_publications_hides_form_144_rows() -> None:
    from datetime import datetime

    from app.services.regulatory_monitoring_service import list_publications_for_user

    apple = RegulatoryPublication(
        id="apple-144",
        source="SEC",
        title="Apple Inc.: 144",
        url="https://www.sec.gov/Archives/edgar/data/320193/0001/xsl144X01/primary_doc.xml",
        published_at=datetime(2026, 8, 10),
        raw_text="Form 144 Filer Information UNITED STATES",
        processed=False,
        embedding_chunk_ids=[],
        created_at=datetime(2026, 8, 10),
    )
    advisory = RegulatoryPublication(
        id="cftc-1",
        source="CFTC",
        title="CFTC Releases Advisory on Self-Certification of Incentive Programs for Prediction Markets",
        url="https://www.cftc.gov/PressRoom/PressReleases/example",
        published_at=datetime(2026, 8, 11),
        raw_text="CFTC Releases Advisory on Self-Certification",
        processed=False,
        embedding_chunk_ids=[],
        created_at=datetime(2026, 8, 11),
    )

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *_args):
            return self

        def options(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def distinct(self):
            return self

        def all(self):
            return self.rows

    class Db:
        def query(self, model, *_rest):
            if "publication_id" in str(model) or "GapAnalysis" in str(model):
                return Query([])
            return Query([apple, advisory])

    listed = list_publications_for_user(Db(), org_id="org-a", is_internal_user=True, limit=50)  # type: ignore[arg-type]
    assert [item.title for item in listed] == [advisory.title]


def test_interleave_source_buckets_puts_each_agency_near_the_top() -> None:
    from types import SimpleNamespace

    from app.services.regulatory_monitoring_service import interleave_source_buckets

    buckets = {
        "SEC": [SimpleNamespace(id="s1"), SimpleNamespace(id="s2"), SimpleNamespace(id="s3")],
        "FINRA": [SimpleNamespace(id="f1")],
        "CFTC": [SimpleNamespace(id="c1")],
    }
    rows = interleave_source_buckets(
        buckets,
        sources=("SEC", "FINRA", "CFTC"),
        limit=5,
    )
    assert [row.id for row in rows] == ["s1", "f1", "c1", "s2", "s3"]


def test_list_publications_for_user_interleaves_sec_finra_cftc() -> None:
    from datetime import datetime

    from app.services.regulatory_monitoring_service import list_publications_for_user

    def _pub(source: str, suffix: str, day: int) -> RegulatoryPublication:
        return RegulatoryPublication(
            id=f"{source.lower()}-{suffix}",
            source=source,
            title=f"{source} {suffix}",
            url=f"https://example.com/{source.lower()}/{suffix}",
            published_at=datetime(2026, 8, day),
            raw_text=f"{source} body {suffix}",
            processed=False,
            embedding_chunk_ids=[],
            created_at=datetime(2026, 8, day),
        )

    queued = [
        [_pub("SEC", "A", 12), _pub("SEC", "B", 11)],
        [_pub("FINRA", "A", 10)],
        [_pub("CFTC", "A", 9)],
        [],
    ]

    class Query:
        def __init__(self, rows: list[RegulatoryPublication] | None = None) -> None:
            self._rows = rows

        def filter(self, *_args):
            return self

        def options(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, *_args):
            return self

        def distinct(self):
            return self

        def all(self):
            if self._rows is not None:
                return self._rows
            if queued:
                return queued.pop(0)
            return []

    class Db:
        def query(self, model, *_rest):
            if "publication_id" in str(model) or "GapAnalysis" in str(model):
                return Query([])
            return Query()

    listed = list_publications_for_user(Db(), org_id="org-a", is_internal_user=True, limit=5)  # type: ignore[arg-type]
    assert [item.source for item in listed] == ["SEC", "FINRA", "CFTC", "SEC"]
    assert [item.title for item in listed] == ["SEC A", "FINRA A", "CFTC A", "SEC B"]


@pytest.mark.asyncio
async def test_sec_scraper_uses_newsroom_rss() -> None:
    newsroom_url = "https://www.sec.gov/news/pressreleases.rss"
    article_url = "https://www.sec.gov/newsroom/press-releases/2026-01-example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>SEC Newsroom Item</title><link>{article_url}</link>
      <description>Newsroom summary.</description>
    </item></channel></rss>"""
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        if str(request.url) == newsroom_url:
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(200, text="<html><main><p>Newsroom body.</p></main></html>")

    settings = Settings(
        SEC_NEWSROOM_URLS=newsroom_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_sec_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].url == article_url
    assert publications[0].title == "SEC Newsroom Item"
    assert "data.sec.gov" not in hosts


@pytest.mark.asyncio
async def test_sec_scraper_skips_issuer_filings_when_newsroom_is_empty() -> None:
    settings = Settings(SEC_NEWSROOM_URLS="", REGULATORY_MAX_ITEMS_PER_FEED=1)
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host or "")
        return httpx.Response(200, json={"name": "Example Issuer"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_sec_publications(client, settings)

    assert publications == []
    assert hosts == []


@pytest.mark.asyncio
async def test_finra_scraper_parses_rss_and_scrapes_full_text() -> None:
    feed_url = "https://feeds.example.test/finra"
    article_url = "https://www.finra.org/rules-guidance/notices/example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>Regulatory Notice 26-01</title><link>{article_url}</link>
      <pubDate>Mon, 22 Jun 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == feed_url:
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(
            200,
            text="<html><nav>Navigation</nav><article><p>Full FINRA notice.</p></article></html>",
        )

    settings = Settings(
        FINRA_RSS_URLS=feed_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "Regulatory Notice 26-01"
    assert publications[0].url == article_url
    assert publications[0].raw_text == "Full FINRA notice."
    assert publications[0].published_at is not None


@pytest.mark.asyncio
async def test_finra_scraper_retries_429_and_recovers(monkeypatch) -> None:
    feed_url = "https://feeds.example.test/finra"
    article_url = "https://www.finra.org/rules-guidance/notices/example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>Regulatory Notice 26-01</title><link>{article_url}</link>
    </item></channel></rss>"""
    article_hits = {"count": 0}
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == feed_url:
            return httpx.Response(200, content=rss.encode())
        article_hits["count"] += 1
        if article_hits["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, text="slow down")
        return httpx.Response(
            200,
            text="<html><article><p>Recovered FINRA notice.</p></article></html>",
        )

    monkeypatch.setattr(
        "app.services.regulatory_monitoring_service.asyncio.sleep",
        fake_sleep,
    )
    settings = Settings(
        FINRA_RSS_URLS=feed_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        FINRA_REQUESTS_PER_SECOND=100,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].raw_text == "Recovered FINRA notice."
    assert article_hits["count"] == 2
    assert 2.0 in sleeps


@pytest.mark.asyncio
async def test_finra_scraper_continues_when_one_feed_fails() -> None:
    bad_url = "https://feeds.example.test/bad"
    good_url = "https://feeds.example.test/good"
    article_url = "https://www.finra.org/rules-guidance/notices/example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>Regulatory Notice 26-02</title><link>{article_url}</link>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == bad_url:
            return httpx.Response(500, text="boom")
        if str(request.url) == good_url:
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(200, text="<html><article><p>Body.</p></article></html>")

    settings = Settings(
        FINRA_RSS_URLS=f"{bad_url},{good_url}",
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        FINRA_REQUESTS_PER_SECOND=100,
        REGULATORY_HTTP_MAX_ATTEMPTS=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "Regulatory Notice 26-02"


def test_retry_after_seconds_prefers_header() -> None:
    response = httpx.Response(429, headers={"Retry-After": "7"})
    assert retry_after_seconds(response, attempt=0) == 7.0


def test_retry_after_seconds_keeps_503_backoff_short() -> None:
    response = httpx.Response(503)
    assert retry_after_seconds(response, attempt=0) == 1.0
    assert retry_after_seconds(response, attempt=3) == 4.0


def test_feed_url_candidates_include_http_fallback_for_finra_feeds() -> None:
    assert feed_url_candidates("https://feeds.finra.org/FINRANews") == [
        "https://feeds.finra.org/FINRANews",
        "http://feeds.finra.org/FINRANews",
    ]
    assert feed_url_candidates("http://feeds.finra.org/FINRANotices") == [
        "http://feeds.finra.org/FINRANotices",
        "https://feeds.finra.org/FINRANotices",
    ]


def test_clean_feed_title_strips_finra_anchor_markup() -> None:
    raw = (
        '<a href="https://www.finra.org/media-center/newsreleases/2026/example" '
        'hreflang="en">FINRA Expels Example Firm</a>'
    )
    assert clean_feed_title(raw) == "FINRA Expels Example Firm"


@pytest.mark.asyncio
async def test_finra_scraper_skips_known_urls_and_pauses_after_429(monkeypatch) -> None:
    feed_url = "http://feeds.example.test/finra"
    known_url = "https://www.finra.org/rules-guidance/notices/known"
    limited_url = "https://www.finra.org/rules-guidance/notices/limited"
    later_url = "https://www.finra.org/rules-guidance/notices/later"
    rss = f"""<?xml version="1.0"?><rss><channel>
      <item><title>Known Notice</title><link>{known_url}</link><description>Known summary</description></item>
      <item><title>Limited Notice</title><link>{limited_url}</link><description>Limited summary</description></item>
      <item><title>Later Notice</title><link>{later_url}</link><description>Later summary</description></item>
    </channel></rss>"""
    article_hits: list[str] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == feed_url:
            return httpx.Response(200, content=rss.encode())
        article_hits.append(url)
        if url == limited_url:
            return httpx.Response(429, headers={"Retry-After": "1"}, text="slow down")
        return httpx.Response(200, text="<html><article><p>Full body</p></article></html>")

    monkeypatch.setattr(
        "app.services.regulatory_monitoring_service.asyncio.sleep",
        fake_sleep,
    )
    settings = Settings(
        FINRA_RSS_URLS=feed_url,
        REGULATORY_MAX_ITEMS_PER_FEED=10,
        FINRA_REQUESTS_PER_SECOND=100,
        REGULATORY_HTTP_MAX_ATTEMPTS=2,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(
            client,
            settings,
            known_urls={known_url},
        )

    assert [item.url for item in publications] == [limited_url, later_url]
    assert known_url not in article_hits
    assert article_hits.count(limited_url) == 2  # attempts=2 then pause
    assert later_url not in article_hits  # paused after rate limit
    assert publications[0].raw_text == "Limited summary"
    assert publications[1].raw_text == "Later summary"


@pytest.mark.asyncio
async def test_finra_scraper_falls_back_from_https_to_http_feed() -> None:
    https_url = "https://feeds.finra.org/FINRANews"
    http_url = "http://feeds.finra.org/FINRANews"
    article_url = "https://www.finra.org/media-center/newsreleases/2026/example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title><![CDATA[<a href="{article_url}">FINRA News Item</a>]]></title>
      <link>{article_url}</link>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == https_url:
            raise httpx.ConnectError("ssl eof", request=request)
        if str(request.url) == http_url:
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(200, text="<html><article><p>Body.</p></article></html>")

    settings = Settings(
        FINRA_RSS_URLS=https_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        FINRA_REQUESTS_PER_SECOND=100,
        REGULATORY_HTTP_MAX_ATTEMPTS=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "FINRA News Item"
    assert publications[0].url == article_url


@pytest.mark.asyncio
async def test_get_with_retries_raises_after_exhausted_429(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"}, text="nope")

    async def fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "app.services.regulatory_monitoring_service.asyncio.sleep",
        fake_sleep,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await get_with_retries(client, "https://example.test/x", attempts=2)


@pytest.mark.asyncio
async def test_finra_media_center_page_scraper_discovers_articles_and_scrapes_full_text() -> None:
    root_url = "https://www.finra.org/media-center"
    article_url = "https://www.finra.org/media-center/newsreleases/2026/finra-notice"
    root_html = f'''<html><body><main>
      <article>
        <a href="{article_url}">FINRA Notice 26-01</a>
        <div>Jun 26, 2026</div>
      </article>
    </main></body></html>'''

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html)
        return httpx.Response(200, text="<html><article><p>FINRA notice body.</p></article></html>")

    settings = Settings(
        FINRA_RSS_URLS=root_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "FINRA Notice 26-01"
    assert publications[0].url == article_url
    assert publications[0].raw_text == "FINRA notice body."
    assert publications[0].published_at is not None


@pytest.mark.asyncio
async def test_sec_newsroom_page_scraper_discovers_articles_and_scrapes_full_text() -> None:
    root_url = "https://www.sec.gov/newsroom"
    article_url = "https://www.sec.gov/newsroom/press-releases/2026-59-sec-cftc-seek-public-comment"
    root_html = f'''<html><body><main>
      <section>
        <a href="{article_url}">SEC Press Release 2026-59</a>
        <div>Jun 26, 2026</div>
      </section>
    </main></body></html>'''

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html)
        return httpx.Response(200, text="<html><article><p>SEC release body.</p></article></html>")

    settings = Settings(
        SEC_NEWSROOM_URLS=root_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_sec_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "SEC Press Release 2026-59"
    assert publications[0].url == article_url
    assert publications[0].raw_text == "SEC release body."
    assert publications[0].published_at is not None


@pytest.mark.asyncio
async def test_cftc_press_room_page_scraper_discovers_listing_and_scrapes_full_text() -> None:
    root_url = "https://www.cftc.gov/PressRoom/index.htm"
    listing_url = "https://www.cftc.gov/PressRoom/PressReleases"
    article_url = "https://www.cftc.gov/PressRoom/PressReleases/9262-26"
    root_html = f'''<html><body><main>
      <a href="{listing_url}">Press Releases</a>
    </main></body></html>'''
    listing_html = f'''<html><body><main>
      <div><span>06/26/2026</span> <a href="{article_url}">CFTC Order 9262-26</a></div>
    </main></body></html>'''

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == root_url:
            return httpx.Response(200, text=root_html)
        if str(request.url) == listing_url:
            return httpx.Response(200, text=listing_html)
        return httpx.Response(200, text="<html><article><p>CFTC release body.</p></article></html>")

    settings = Settings(
        CFTC_RSS_URLS=root_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=True,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_cftc_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].title == "CFTC Order 9262-26"
    assert publications[0].url == article_url
    assert publications[0].raw_text == "CFTC release body."
    assert publications[0].published_at is not None


@pytest.mark.asyncio
async def test_cftc_rss_fetches_article_when_description_empty() -> None:
    feed_url = "https://feeds.example.test/cftc.rss"
    article_url = "https://www.cftc.gov/PressRoom/PressReleases/9268-26"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>CFTC Press Release</title><link>{article_url}</link>
      <description></description>
    </item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == feed_url:
            return httpx.Response(200, content=rss.encode())
        return httpx.Response(
            200,
            text="<html><article><p>CFTC article body from HTML.</p></article></html>",
        )

    settings = Settings(
        CFTC_RSS_URLS=feed_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_cftc_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].raw_text == "CFTC article body from HTML."


@pytest.mark.asyncio
async def test_cftc_press_room_hub_fails_soft_without_publications() -> None:
    hub_url = "https://www.cftc.gov/PressRoom/index.htm"
    hub_html = """<html><body><main>
      <a href="/PressRoom/PressReleases">Press Releases</a>
      <a href="/PressRoom/PressReleases/index.htm">See the latest news from CFTC.</a>
    </main></body></html>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == hub_url:
            return httpx.Response(200, text=hub_html)
        if str(request.url).endswith("/PressRoom/PressReleases"):
            return httpx.Response(
                200,
                text="<html><body><main><p>No numbered releases here.</p></main></body></html>",
            )
        raise AssertionError(f"unexpected url {request.url}")

    settings = Settings(
        CFTC_RSS_URLS=hub_url,
        REGULATORY_MAX_ITEMS_PER_FEED=5,
        REGULATORY_FETCH_FULL_TEXT=False,
        REGULATORY_HTTP_MAX_ATTEMPTS=1,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_cftc_publications(client, settings)

    assert publications == []

    feed_url = "https://feeds.example.test/finra"
    article_url = "https://www.finra.org/rules-guidance/notices/example"
    rss = f"""<?xml version="1.0"?><rss><channel><item>
      <title>Regulatory Notice 26-01</title><link>{article_url}</link>
      <description>&lt;p&gt;Summary only body.&lt;/p&gt;</description>
    </item></channel></rss>"""
    article_hits = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == feed_url:
            return httpx.Response(200, content=rss.encode())
        article_hits["count"] += 1
        return httpx.Response(200, text="<html><article><p>Should not fetch.</p></article></html>")

    settings = Settings(
        FINRA_RSS_URLS=feed_url,
        REGULATORY_MAX_ITEMS_PER_FEED=1,
        REGULATORY_FETCH_FULL_TEXT=False,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publications = await scrape_finra_publications(client, settings)

    assert len(publications) == 1
    assert publications[0].raw_text == "Summary only body."
    assert article_hits["count"] == 0


def test_default_sec_newsroom_urls_prefer_rss_feeds() -> None:
    sec_default = Settings.model_fields["sec_newsroom_urls"].default
    finra_default = Settings.model_fields["finra_rss_urls"].default
    cftc_default = Settings.model_fields["cftc_rss_urls"].default
    assert "https://www.sec.gov/newsroom" in sec_default
    assert "https://www.sec.gov/news/pressreleases.rss" in sec_default
    assert "https://www.sec.gov/news/speeches-statements.rss" in sec_default
    assert "https://www.finra.org/media-center" in finra_default
    assert "http://feeds.finra.org/FINRANotices" in finra_default
    assert "https://www.cftc.gov/PressRoom/index.htm" in cftc_default
    assert "https://www.cftc.gov/RSS/RSSGP/rssgp.xml" in cftc_default
    assert Settings.model_fields["regulatory_fetch_full_text"].default is False

def test_scope_matching_and_html_extraction_are_source_specific() -> None:
    organization = Organization(
        id="org-db-id",
        auth_org_id="org_example",
        name="Example Capital",
        regulatory_scope={"sources": ["finra", "CFTC"]},
    )

    assert publication_matches_organization("FINRA", organization) is True
    assert publication_matches_organization("SEC", organization) is False
    assert extract_html_text("<main><script>ignore()</script><p>Readable text.</p></main>") == "Readable text."


def test_organization_source_scope_supports_legacy_array_format() -> None:
    organization = Organization(
        id="org-db-id",
        auth_org_id="org_example",
        name="Example Capital",
        regulatory_scope=["SEC", "FINRA"],
    )

    assert publication_matches_organization("FINRA", organization) is True
    assert publication_matches_organization("SEC", organization) is True
    assert publication_matches_organization("CFTC", organization) is False


def test_persist_new_publications_deduplicates_existing_and_new_urls() -> None:
    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return [
                RegulatoryPublication(
                    id="existing",
                    source="FINRA",
                    title="Existing",
                    url="https://example.test/existing",
                    raw_text="Existing notice body that is already long enough to keep.",
                    processed=False,
                    embedding_chunk_ids=[],
                )
            ]

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.committed = False

        def query(self, *_args):
            return Query()

        def begin_nested(self):
            return nullcontext()

        def add(self, value: object) -> None:
            self.added.append(value)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            return None

    publications = [
        ScrapedPublication("FINRA", "Existing", "https://example.test/existing", None, None),
        ScrapedPublication("FINRA", "New", "https://example.test/new", None, "Full text"),
    ]
    db = RecordingSession()

    created, duplicates = persist_new_publications(db, publications)  # type: ignore[arg-type]

    assert len(created) == 1
    assert isinstance(db.added[0], RegulatoryPublication)
    assert db.added[0].url == "https://example.test/new"
    assert duplicates == 1
    assert db.committed is True


def test_persist_new_publications_refreshes_title_only_raw_text() -> None:
    existing = RegulatoryPublication(
        id="existing-thin",
        source="CFTC",
        title="CFTC Issues Final Rule",
        url="https://example.test/final-rule",
        raw_text="CFTC Issues Final Rule",
        processed=False,
        embedding_chunk_ids=[],
    )

    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return [existing]

    class RecordingSession:
        def query(self, *_args):
            return Query()

        def begin_nested(self):
            return nullcontext()

        def add(self, value: object) -> None:
            raise AssertionError(f'unexpected insert {value!r}')

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    created, duplicates = persist_new_publications(
        RecordingSession(),  # type: ignore[arg-type]
        [
            ScrapedPublication(
                "CFTC",
                "CFTC Issues Final Rule",
                "https://example.test/final-rule",
                None,
                "The CFTC issued a final rule modifying the clearing requirement for CAD and MXN swaps. " * 4,
            )
        ],
    )

    assert created == []
    assert duplicates == 1
    assert existing.raw_text is not None
    assert existing.raw_text.startswith("The CFTC issued a final rule")
    assert existing.feed_summary is None


def test_persist_new_publications_refreshes_thin_raw_text() -> None:
    existing = RegulatoryPublication(
        id="existing-short-blurb",
        source="CFTC",
        title="CFTC Issues Final Rule to Modify Clearing Requirement",
        url="https://example.test/final-rule-thin",
        raw_text="Short RSS blurb for a CFTC final rule.",
        feed_summary="Firms should document their implementation review i...",
        processed=False,
        embedding_chunk_ids=[],
    )

    class Query:
        def filter(self, *_args):
            return self

        def all(self):
            return [existing]

    class RecordingSession:
        def query(self, *_args):
            return Query()

        def begin_nested(self):
            return nullcontext()

        def add(self, value: object) -> None:
            raise AssertionError(f'unexpected insert {value!r}')

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    created, duplicates = persist_new_publications(
        RecordingSession(),  # type: ignore[arg-type]
        [
            ScrapedPublication(
                "CFTC",
                "CFTC Issues Final Rule to Modify Clearing Requirement",
                "https://example.test/final-rule-thin",
                None,
                "The CFTC issued a final rule modifying the clearing requirement for CAD and MXN swaps. " * 8,
            )
        ],
    )

    assert created == []
    assert duplicates == 1
    assert existing.raw_text is not None
    assert existing.raw_text.startswith("The CFTC issued a final rule")
    assert existing.feed_summary is None


def test_publication_summary_replaces_title_only_feed_summary() -> None:
    body = "The CFTC issued a final rule modifying the clearing requirement. " * 40
    publication = RegulatoryPublication(
        id="pub-title-summary",
        source="CFTC",
        title="CFTC Issues Final Rule to Modify Clearing Requirement",
        url="https://example.test/title-summary",
        raw_text=body,
        feed_summary="CFTC Issues Final Rule to Modify Clearing Requirement",
        processed=False,
        embedding_chunk_ids=[],
    )
    summary = publication_summary(publication)
    assert summary is not None
    assert summary != publication.title
    assert summary.startswith("The CFTC issued a final rule")


def test_publication_text_is_thin_for_title_only_bodies() -> None:
    from app.services.publication_summary_service import (
        publication_text_is_thin,
        publication_text_is_title_only,
    )

    title = "CFTC Issues Final Rule to Modify Clearing Requirement"
    assert publication_text_is_title_only(title, title) is True
    assert publication_text_is_title_only("Summary only body.", title) is False
    assert publication_text_is_thin(title, title) is True
    assert publication_text_is_thin("short", title) is True
    assert publication_text_is_thin("The CFTC issued a final rule. " * 20, title) is False


@pytest.mark.asyncio
async def test_publication_event_is_ignored_by_compliance_workflow() -> None:
    context = SimpleNamespace(
        event=SimpleNamespace(data={"publication_id": "publication-id", "org_id": "org_example"})
    )

    response = await publication_created_handler(context)

    assert response == {
        'status': 'ignored',
        'message': 'Publication gap analysis is handled by the regulatory compliance workflow cron.',
    }


@pytest.mark.asyncio
async def test_publication_event_without_required_data_is_ignored() -> None:
    context = SimpleNamespace(event=SimpleNamespace(data={}))

    response = await publication_created_handler(context)

    assert response == {
        'status': 'ignored',
        'message': 'Publication gap analysis is handled by the regulatory compliance workflow cron.',
    }


def test_publication_feed_status_ingested_and_matched() -> None:
    from app.services.regulatory_monitoring_service import publication_feed_status

    saved = RegulatoryPublication(
        id="pub-1",
        source="SEC",
        title="Remarks at the Society for Corporate Governance Conference",
        url="https://example.test/1",
        raw_text="Speech body",
        processed=False,
        embedding_chunk_ids=[],
    )
    without_text = RegulatoryPublication(
        id="pub-2",
        source="SEC",
        title="Notice",
        url="https://example.test/2",
        raw_text=None,
        processed=False,
        embedding_chunk_ids=[],
    )

    assert publication_feed_status(saved) == "ingested"
    assert publication_feed_status(without_text) == "ingested"
    assert publication_feed_status(saved, matched=True) == "matched"


def test_publication_summary_keeps_complete_llm_brief_over_raw_excerpt() -> None:
    body = "More in this Section Statement page chrome. " + ("word " * 400)
    brief = (
        "The Commission proposed to rescind Rule 206(4)-5 and asked advisers to review "
        "pay-to-play procedures, recordkeeping, and solicitation arrangements. "
    ) * 6
    publication = RegulatoryPublication(
        id="pub-keep-llm",
        source="SEC",
        title="Statement on Proposal to Rescind Pay-to-Play Rule",
        url="https://example.test/keep-llm",
        raw_text=body,
        feed_summary=brief.strip(),
        processed=False,
        embedding_chunk_ids=[],
    )
    assert publication_summary(publication) == publication.feed_summary
    assert not (publication_summary(publication) or "").startswith("More in this Section")


def test_publication_summary_prefers_llm_feed_summary() -> None:
    publication = RegulatoryPublication(
        id="pub-summary",
        source="CFTC",
        title="CFTC Orders Example Firm to Pay a Penalty",
        url="https://example.test/order",
        raw_text="CFTC Orders Example Firm to Pay a Penalty " + ("body " * 200),
        feed_summary=(
            "The CFTC ordered Example Firm to pay a civil penalty for insider trading "
            "in event contracts. The order describes the conduct, the product market, "
            "and supervisory failures a CCO should review against WSPs and communications."
        ),
        processed=False,
        embedding_chunk_ids=[],
    )
    assert publication_summary(publication) == publication.feed_summary


def test_publication_summary_replaces_clipped_excerpt_with_longer_body() -> None:
    body = "word " * 500
    publication = RegulatoryPublication(
        id="pub-clipped",
        source="SEC",
        title="Title",
        url="https://example.test/clipped",
        raw_text=body,
        feed_summary="The Securities and Exchange Commission today charged a firm with fraud that…",
        processed=False,
        embedding_chunk_ids=[],
    )
    summary = publication_summary(publication)
    assert summary is not None
    assert len(summary) > len(publication.feed_summary or "")
    assert not summary.startswith("The Securities and Exchange Commission today charged a firm")


def test_publication_summary_excerpt_matches_feed_brief_length() -> None:
    body = "word " * 500
    publication = RegulatoryPublication(
        id="pub-excerpt",
        source="SEC",
        title="Title",
        url="https://example.test/excerpt",
        raw_text=body,
        processed=False,
        embedding_chunk_ids=[],
    )
    summary = publication_summary(publication)
    assert summary is not None
    assert "word" in summary
    assert not summary.endswith("…")
    assert not summary.endswith("...")


def test_feed_summary_needs_refresh_for_short_stored_text() -> None:
    from app.services.publication_summary_service import feed_summary_needs_refresh

    assert feed_summary_needs_refresh(None) is True
    assert feed_summary_needs_refresh("short") is True
    assert feed_summary_needs_refresh("x" * 1_200) is False
    assert feed_summary_needs_refresh(("Complete sentence. " * 80) + "cut off i...") is True


def test_should_replace_feed_summary_keeps_clipped_brief_over_thin_excerpt() -> None:
    from app.services.publication_summary_service import should_replace_feed_summary

    clipped = ("The CFTC issued a final rule. " * 40) + "implementation review i..."
    title = "CFTC Issues Final Rule"
    assert should_replace_feed_summary(clipped, "Short RSS blurb.", title) is False
    complete = "The CFTC issued a final rule modifying the clearing requirement. " * 20
    assert should_replace_feed_summary(clipped, complete, title) is True
    assert should_replace_feed_summary(None, complete, title) is True


@pytest.mark.asyncio
async def test_generate_publication_feed_summary_uses_llm_json() -> None:
    from app.services.publication_summary_service import generate_publication_feed_summary

    settings = Settings(ANTHROPIC_API_KEY="test-key")
    client = FakeLLM('{"summary": "' + ("Complete reader summary. " * 20) + '"}')
    summary = await generate_publication_feed_summary(
        title="CFTC Orders Example Firm to Pay a Penalty",
        source="CFTC",
        url="https://example.test/order",
        raw_text="The Commission found insider trading in event contracts and imposed a penalty.",
        settings=settings,
        client=client,
    )
    assert summary is not None
    assert "Complete reader summary" in summary
    assert len(summary) > 100
    assert client.calls[0]["throttle"] == "sonnet"
    assert client.calls[0]["throttle_budget"] is False

