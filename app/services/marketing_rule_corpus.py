"""Fetch canonical SEC/FINRA marketing rule text from public official sources."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import structlog
from fastapi import HTTPException

from app.core.config import Settings
from app.services.document_ingestion import normalize_text
from app.services.marketing_ingest_service import assert_safe_public_url

logger = structlog.get_logger(__name__)

_ALLOWED_RULE_HOSTS = frozenset({
    'www.ecfr.gov',
    'ecfr.gov',
    'www.finra.org',
    'finra.org',
    'www.law.cornell.edu',
})
_CACHE_TTL_SECONDS = 6 * 3600
_MIN_RULE_CHARS = 280
_MAX_RULE_CHARS = 80_000
_FETCH_TIMEOUT_SECONDS = 20.0
_FINRA_RULEBOOK_REFERER = 'https://www.finra.org/rules-guidance/rulebooks/finra-rules'
_CHROME_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
)
_SAFARI_UA = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/17.0 Safari/605.1.15'
)

_BROWSER_HEADERS = {
    'User-Agent': _CHROME_UA,
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    regulator: str
    title: str
    urls: tuple[str, ...]


@dataclass(frozen=True)
class RuleDocument:
    rule_id: str
    regulator: str
    title: str
    url: str
    text: str
    source: str  # live | fallback

    def as_metadata_dict(self) -> dict[str, str]:
        return {
            'rule_id': self.rule_id,
            'regulator': self.regulator,
            'title': self.title,
            'url': self.url,
            'source': self.source,
        }


_RULE_CACHE: dict[str, tuple[float, RuleDocument]] = {}
_FINRA_FETCH_LOCK = asyncio.Lock()
_FALLBACK_CACHE_TTL_SECONDS = 120


def _ecfr_xml(section: str) -> str:
    today = datetime.now(UTC).date().isoformat()
    return (
        f'https://www.ecfr.gov/api/versioner/v1/full/{today}/title-17.xml'
        f'?part=275&section={section}'
    )


RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec(
        '206(4)-1',
        'SEC',
        'SEC Rule 206(4)-1, the Marketing Rule',
        (
            _ecfr_xml('275.206(4)-1'),
            'https://www.ecfr.gov/current/title-17/chapter-II/part-275/section-275.206(4)-1',
            'https://www.law.cornell.edu/cfr/text/17/275.206(4)-1',
        ),
    ),
    RuleSpec(
        '204-2',
        'SEC',
        'SEC Rule 204-2, recordkeeping',
        (
            _ecfr_xml('275.204-2'),
            'https://www.ecfr.gov/current/title-17/chapter-II/part-275/section-275.204-2',
            'https://www.law.cornell.edu/cfr/text/17/275.204-2',
        ),
    ),
    RuleSpec(
        '2210',
        'FINRA',
        'FINRA Rule 2210, communications with the public',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210',),
    ),
    RuleSpec(
        '2211',
        'FINRA',
        'FINRA Rule 2211, communications about variable contracts',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2211',),
    ),
    RuleSpec(
        '2212',
        'FINRA',
        'FINRA Rule 2212, investment company rankings',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2212',),
    ),
    RuleSpec(
        '2213',
        'FINRA',
        'FINRA Rule 2213, bond mutual fund volatility ratings',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2213',),
    ),
    RuleSpec(
        '2214',
        'FINRA',
        'FINRA Rule 2214, investment analysis tools',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2214',),
    ),
    RuleSpec(
        '2215',
        'FINRA',
        'FINRA Rule 2215, communications about security futures',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2215',),
    ),
    RuleSpec(
        '2216',
        'FINRA',
        'FINRA Rule 2216, communications about collateralized mortgage obligations',
        ('https://www.finra.org/rules-guidance/rulebooks/finra-rules/2216',),
    ),
)

# Policy Library / regulations.rule_code groups. 2211–2216 are one downloadable document.
MARKETING_CATALOG_GROUPS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        '206(4)-1',
        'SEC',
        'SEC: Rule 206(4)-1: The Marketing Rule',
        ('206(4)-1',),
    ),
    (
        '204-2',
        'SEC',
        'SEC: Rule 204-2: Recordkeeping',
        ('204-2',),
    ),
    (
        '2210',
        'FINRA',
        'FINRA: Rule 2210: Communications with the public',
        ('2210',),
    ),
    (
        '2211-2216',
        'FINRA',
        'FINRA: Rules 2211–2216',
        ('2211', '2212', '2213', '2214', '2215', '2216'),
    ),
)

# Used only when every official URL fails. Distinctive public-rule summaries,
# not a substitute for the live eCFR / FINRA rulebook text.
_FALLBACK_TEXT: dict[str, str] = {
    '206(4)-1': (
        '17 CFR 275.206(4)-1, the SEC Marketing Rule. An advertisement may not '
        'include an untrue statement of a material fact or omit a material fact '
        'necessary to make the statement not misleading. Testimonials and '
        'endorsements require clear and prominent disclosures of whether the '
        'person is a client and whether cash or non-cash compensation was provided. '
        'Third-party ratings require disclosure of the date, period, and whether '
        'compensation was paid. Performance advertising must present net '
        'performance with equal prominence to gross performance. Hypothetical '
        'performance and extracted performance have additional conditions. '
        'Disclosures must be clear and prominent, not buried.'
    ),
    '204-2': (
        '17 CFR 275.204-2, books and records. An investment adviser must make and '
        'keep true, accurate, and current records of advertisements, including a '
        'copy of each advertisement and the documents necessary to substantiate '
        'performance. Marketing reviews must be retained and findable, including '
        'which version of the rule was used when the advertisement was reviewed.'
    ),
    '2210': (
        'FINRA Rule 2210, communications with the public. Retail communications, '
        'correspondence, and institutional communications must be fair and balanced '
        'and must not omit material facts. Retail communications generally require '
        'principal approval before use and may require filing. Records of '
        'communications must be retained. Content standards prohibit false, '
        'exaggerated, or misleading statements, including promissory language.'
    ),
    '2211': (
        'FINRA Rule 2211 applies to communications concerning variable contracts, '
        'including variable annuities. Such communications must disclose that the '
        'product is an insurance contract, describe fees and surrender charges, '
        'and must not present the variable annuity as a short-term liquid investment.'
    ),
    '2212': (
        'FINRA Rule 2212 governs use of investment company rankings, including Lipper '
        'rankings and Morningstar ratings, in retail communications. Rankings must '
        'come from a ranking entity, cover at least one year, and disclose the '
        'category, period, and number of companies ranked.'
    ),
    '2213': (
        'FINRA Rule 2213 governs bond mutual fund volatility ratings in sales material. '
        'A volatility rating may be used only with required disclosures about the '
        'methodology, the rating date, and that the rating is not a credit rating.'
    ),
    '2214': (
        'FINRA Rule 2214 covers investment analysis tools, including portfolio '
        'analyzers and calculators that produce simulated outcomes. The tool and '
        'related retail communications must describe criteria, limitations, and that '
        'results may vary.'
    ),
    '2215': (
        'FINRA Rule 2215 applies to communications about security futures. Such '
        'communications must disclose that security futures are not suitable for all '
        'investors and that they involve a high degree of risk, including the risk '
        'of losing more than the initial investment.'
    ),
    '2216': (
        'FINRA Rule 2216 applies to communications about collateralized mortgage '
        'obligations (CMOs) and mortgage-backed securities. Communications must '
        'disclose that CMOs have unique risks, including prepayment and extension '
        'risk, and must not compare CMOs to other investments without required context.'
    ),
}


def _assert_rule_url(url: str) -> str:
    safe = assert_safe_public_url(url)
    host = (urlparse(safe).hostname or '').lower().rstrip('.')
    if host not in _ALLOWED_RULE_HOSTS:
        raise HTTPException(status_code=400, detail='rule source host is not allowed')
    return safe


def _is_challenge_page(raw: bytes) -> bool:
    head = raw[:2_000].decode('utf-8', errors='replace').lower()
    return 'just a moment' in head or 'cf-browser-verification' in head or 'cdn-cgi/challenge-platform' in head


def _request_headers(url: str) -> dict[str, str]:
    headers = dict(_BROWSER_HEADERS)
    host = (urlparse(url).hostname or '').lower()
    if host.endswith('finra.org'):
        headers['User-Agent'] = _SAFARI_UA
        headers['Referer'] = _FINRA_RULEBOOK_REFERER
    else:
        headers['User-Agent'] = _CHROME_UA
    return headers


def _trim_rule_preamble(text: str, rule_id: str = '') -> str:
    """Drop site chrome (eCFR browser banners) before the rule text."""
    cleaned = re.sub(
        r'(site feedback\s+)?you are using an unsupported browser[.\s×x]*',
        ' ',
        text,
        flags=re.IGNORECASE,
    )
    cleaned = normalize_text(cleaned)
    markers: tuple[str, ...] = ()
    if rule_id == '206(4)-1':
        markers = ('§ 275.206(4)-1',)
    elif rule_id == '204-2':
        markers = ('§ 275.204-2',)
    elif rule_id:
        markers = (f'{rule_id}. ',)
    lowered = cleaned.lower()
    for marker in markers:
        idx = lowered.find(marker.lower())
        if idx > 0:
            return cleaned[idx:]
    return cleaned


def html_or_xml_to_text(raw: bytes, content_type: str = '', rule_id: str = '') -> str:
    """Turn an official rule page (HTML or XML) into visible text."""
    sniff = (content_type or '').lower()
    payload = raw.decode('utf-8', errors='replace')
    looks_xml = 'xml' in sniff or payload.lstrip().startswith('<?xml')
    if looks_xml:
        xml_text = _trim_rule_preamble(_xml_to_text(raw), rule_id)
        if len(xml_text) >= _MIN_RULE_CHARS:
            return xml_text[:_MAX_RULE_CHARS]
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _trim_rule_preamble(normalize_text(_strip_tags(payload)), rule_id)[:_MAX_RULE_CHARS]
    soup = BeautifulSoup(payload, 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'template', 'svg', 'nav', 'iframe']):
        tag.decompose()
    root = soup.find('main') or soup.find(id='content') or soup.body or soup
    text = normalize_text(root.get_text(' ', strip=True) if root is not None else '')
    return _trim_rule_preamble(text, rule_id)[:_MAX_RULE_CHARS]


def _xml_to_text(raw: bytes) -> str:
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return ''
    parts: list[str] = []
    for elem in root.iter():
        if elem.text and elem.text.strip():
            parts.append(elem.text.strip())
        if elem.tail and elem.tail.strip():
            parts.append(elem.tail.strip())
    return normalize_text(' '.join(parts))


def _strip_tags(payload: str) -> str:
    import re
    return re.sub(r'<[^>]+>', ' ', payload)


def fallback_rule_document(spec: RuleSpec) -> RuleDocument:
    return RuleDocument(
        rule_id=spec.rule_id,
        regulator=spec.regulator,
        title=spec.title,
        url=spec.urls[0],
        text=_FALLBACK_TEXT[spec.rule_id],
        source='fallback',
    )


def fallback_rule_corpus(specs: tuple[RuleSpec, ...] | None = None) -> list[RuleDocument]:
    """In-memory SEC/FINRA summaries — used for short-form reviews so we skip live fetches."""
    return [fallback_rule_document(spec) for spec in (specs or RULE_SPECS)]


async def fetch_url_bytes(url: str, settings: Settings) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for delay in (0.0, 2.5, 6.0):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await _fetch_url_bytes_once(url, settings)
        except HTTPException as exc:
            last_error = exc
            detail = str(exc.detail)
            if 'status=429' not in detail and 'challenge=True' not in detail:
                raise
    assert last_error is not None
    raise last_error


async def _fetch_url_bytes_once(url: str, settings: Settings) -> tuple[bytes, str]:
    import httpx

    safe = _assert_rule_url(url)
    max_bytes = min(settings.max_policy_upload_mb * 1024 * 1024, 2_000_000)
    current = safe
    response = None
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        for _ in range(5):
            _assert_rule_url(current)
            response = await client.get(current, headers=_request_headers(current))
            if response.is_redirect:
                location = response.headers.get('location') or ''
                current = urljoin(current, location)
                continue
            break
        else:
            raise HTTPException(status_code=400, detail='rule URL redirected too many times')
    if response is None or response.status_code >= 400 or _is_challenge_page(response.content):
        status = getattr(response, 'status_code', None)
        challenge = bool(response is not None and _is_challenge_page(response.content))
        raise HTTPException(
            status_code=422,
            detail=f'Unable to fetch rule URL (status={status}, challenge={challenge})',
        )
    content_type = response.headers.get('content-type', '')
    return response.content[:max_bytes], content_type


async def fetch_rule_document(spec: RuleSpec, settings: Settings) -> RuleDocument:
    cached = _RULE_CACHE.get(spec.rule_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    if spec.regulator == 'FINRA':
        async with _FINRA_FETCH_LOCK:
            await asyncio.sleep(0.8)
            return await _fetch_rule_document_uncached(spec, settings)
    return await _fetch_rule_document_uncached(spec, settings)


async def _fetch_rule_document_uncached(spec: RuleSpec, settings: Settings) -> RuleDocument:
    now = time.monotonic()
    last_error: Exception | None = None
    for url in spec.urls:
        try:
            raw, content_type = await fetch_url_bytes(url, settings)
            text = html_or_xml_to_text(raw, content_type, rule_id=spec.rule_id)
            if len(text) < _MIN_RULE_CHARS:
                continue
            document = RuleDocument(
                rule_id=spec.rule_id,
                regulator=spec.regulator,
                title=spec.title,
                url=url,
                text=text,
                source='live',
            )
            _RULE_CACHE[spec.rule_id] = (now + _CACHE_TTL_SECONDS, document)
            return document
        except Exception as exc:
            last_error = exc
            logger.info(
                'marketing_rule_fetch_miss',
                rule_id=spec.rule_id,
                error_type=type(exc).__name__,
                error=str(exc)[:180],
            )
            continue
    if last_error is not None:
        logger.warning(
            'marketing_rule_fetch_fallback',
            rule_id=spec.rule_id,
            error_type=type(last_error).__name__,
        )
    document = fallback_rule_document(spec)
    _RULE_CACHE[spec.rule_id] = (now + _FALLBACK_CACHE_TTL_SECONDS, document)
    return document


def catalog_rule_documents(documents: list[RuleDocument]) -> list[RuleDocument]:
    """Collapse live fetches into the four Policy Library marketing-rule documents."""
    by_id = {item.rule_id: item for item in documents}
    grouped: list[RuleDocument] = []
    for catalog_id, regulator, title, member_ids in MARKETING_CATALOG_GROUPS:
        members = [by_id[member_id] for member_id in member_ids if member_id in by_id]
        if not members:
            continue
        if len(members) == 1:
            member = members[0]
            grouped.append(
                RuleDocument(
                    rule_id=catalog_id,
                    regulator=regulator,
                    title=title,
                    url=member.url,
                    text=member.text,
                    source=member.source,
                )
            )
            continue
        text = '\n\n'.join(f'{member.title}\n{member.text}' for member in members)
        live = all(member.source == 'live' for member in members)
        grouped.append(
            RuleDocument(
                rule_id=catalog_id,
                regulator=regulator,
                title=title,
                url=members[0].url,
                text=text,
                source='live' if live else 'fallback',
            )
        )
    return grouped


async def load_marketing_rule_corpus(
    settings: Settings,
    *,
    specs: tuple[RuleSpec, ...] | None = None,
) -> list[RuleDocument]:
    """Fetch SEC 206(4)-1, 204-2, FINRA 2210, and 2211–2216 in parallel."""
    selected = specs or RULE_SPECS
    gathered = await asyncio.gather(
        *[fetch_rule_document(spec, settings) for spec in selected],
        return_exceptions=True,
    )
    documents: list[RuleDocument] = []
    for spec, item in zip(selected, gathered, strict=True):
        if isinstance(item, RuleDocument):
            documents.append(item)
        else:
            logger.warning(
                'marketing_rule_corpus_item_failed',
                rule_id=spec.rule_id,
                error_type=type(item).__name__,
            )
            documents.append(fallback_rule_document(spec))
    return documents


def clear_rule_corpus_cache() -> None:
    _RULE_CACHE.clear()
