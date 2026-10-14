"""Ingest marketing files/URLs with size, type, and SSRF guards."""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import structlog
from fastapi import HTTPException, status

from app.core.config import Settings
from app.services.document_ingestion import extract_docx_sections, extract_pdf_sections, normalize_text
from app.services.marketing_slide_service import (
    extract_presentation_slides,
    is_image_file,
    is_pptx,
    joined_slide_text,
    layout_notes_from_slides,
    sniff_image_media_type,
)
from app.services.marketing_types import (
    ALLOWED_UPLOAD_SUFFIXES,
    ALLOWED_UPLOAD_TYPES,
    FORMATS,
    MAX_BODY_CHARS,
)

logger = structlog.get_logger(__name__)
_DECODED_IMAGE_HEAD_RE = re.compile(
    r'(?:'
    r'(?:^|(?<=\n))(?:\ufffd|\s)*PNG\b'
    r'|PNG\s*IHDR'
    r'|\bIHDR'
    r'|\bIDAT\b'
    r'|\biCCP'
    r'|ICC Profile'
    r'|\bJFIF\b'
    r'|\bGIF8[79]a\b'
    r')',
    re.I,
)
_PRIVATE_HOSTS = frozenset({'localhost', '127.0.0.1', '::1', '0.0.0.0', 'metadata.google.internal'})
_RATING_HINT_RE = re.compile(
    r'\b(forbes|five star|bbb|best[- ]in[- ]state|top\s+\d+|rated|wealth manager)\b',
    re.I,
)
_SAFETY_HINT_RE = re.compile(
    r'protected client capital|every downturn|guarantee|eliminate[s]? (?:all )?risk',
    re.I,
)
_FONT_SIZE_RE = re.compile(r'font-size\s*:\s*([\d.]+)\s*(pt|px)', re.I)
_HTML_PREVIEW_CHARS = 400_000
_JS_URL_RE = re.compile(r'^\s*javascript:', re.I)
_LAYOUT_EXCERPT_CHARS = 12_000
_BLOCK_TAGS = ('h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'li', 'blockquote', 'dt', 'dd', 'figcaption')
_LEAF_COPY_RE = re.compile(
    r'title|description|subtitle|headline|copy|claim|stat|benefit|'
    r'cta|eyebrow|dek|lead|button__text|btn[-_]?text|card-text|promo',
    re.I,
)
_HTTP_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)
_WWW_URL_RE = re.compile(r'\bwww\.[^\s<>"\']+', re.I)
_QUERY_TRACKING_RE = re.compile(
    r'\?[^\s<>"\']*(?:utm_|hsa_|gad_|gclid|fbclid|msclkid)[^\s<>"\']*',
    re.I,
)


def normalize_format(value: str | None, *, filename: str = '', content_type: str = '') -> str:
    raw = (value or '').strip().lower()
    aliases = {
        'webpage': 'webpage',
        'website': 'webpage',
        'homepage': 'webpage',
        'web': 'webpage',
        'presentation': 'presentation',
        'deck': 'presentation',
        'pitch': 'presentation',
        'social': 'social',
        'social_post': 'social',
        'linkedin': 'social',
        'email': 'email',
        'blog': 'blog',
        'article': 'blog',
        'text': 'text',
        'image': 'image',
        'ad': 'text',
        'other': 'text',
    }
    if raw in aliases:
        return aliases[raw]
    suffix = Path(filename).suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    if suffix in {'.png', '.jpg', '.jpeg', '.webp'} or mime.startswith('image/'):
        return 'image'
    if suffix == '.pdf' or mime == 'application/pdf':
        return 'presentation'
    if suffix == '.pptx' or mime == 'application/vnd.openxmlformats-officedocument.presentationml.presentation':
        return 'presentation'
    if suffix in {'.html', '.htm'} or mime == 'text/html':
        return 'email'
    return 'text'


def validate_upload_meta(*, filename: str, content_type: str | None, size: int, max_mb: int) -> None:
    if size <= 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='Marketing file is empty')
    if size > max_mb * 1024 * 1024:
        raise HTTPException(
            413,
            detail=f'Marketing file exceeds the {max_mb} MB upload limit',
        )
    suffix = Path(filename or 'marketing.txt').suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    allowed_suffix = suffix in ALLOWED_UPLOAD_SUFFIXES
    allowed_mime = mime in ALLOWED_UPLOAD_TYPES
    if suffix == '.pptx' or mime in {
        'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
        'application/vnd.ms-powerpoint',
    }:
        return
    if (suffix and not allowed_suffix) and not allowed_mime:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail='Unsupported marketing file type. Use text, HTML, PDF, PowerPoint, Word, or an image.',
        )


def is_decoded_image_dump(text: str | None) -> bool:
    """True when UTF-8 decoding of a PNG/JPEG leaked into marketing copy."""
    head = (text or '')[:1200]
    if not head.strip():
        return False
    return bool(_DECODED_IMAGE_HEAD_RE.search(head))


def readable_marketing_body(body_text: str | None, notes: list[dict[str, object]] | None = None) -> str:
    """Prefer transcribed copy over a decoded PNG dump or empty ingest placeholder."""
    raw = (body_text or '').strip()
    if raw and not is_decoded_image_dump(raw):
        return raw
    excerpts: list[str] = []
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        excerpt = str(note.get('excerpt') or '').strip()
        if excerpt and not is_decoded_image_dump(excerpt):
            excerpts.append(excerpt)
    return '\n\n'.join(excerpts)


def checksum_sha256(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def extract_marketing_text(*, filename: str, content_type: str | None, contents: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    if suffix == '.pdf' or mime == 'application/pdf' or is_pptx(filename, mime):
        slides = extract_presentation_slides(
            filename=filename, content_type=content_type, contents=contents
        )
        if slides:
            return joined_slide_text(slides)[:MAX_BODY_CHARS]
        if suffix == '.pdf' or mime == 'application/pdf':
            sections = extract_pdf_sections(contents)
            return normalize_text('\n\n'.join(section.text for section in sections))[:MAX_BODY_CHARS]
        return ''
    if suffix in {'.doc', '.docx'} or 'wordprocessingml' in mime:
        sections = extract_docx_sections(contents)
        return normalize_text('\n\n'.join(section.text for section in sections))[:MAX_BODY_CHARS]
    if is_image_file(filename=filename, content_type=mime, contents=contents):
        return ''
    if b'\x00' in contents[:1024]:
        return ''
    try:
        decoded = normalize_text(contents.decode('utf-8', errors='replace'))[:MAX_BODY_CHARS]
    except Exception:
        return ''
    if is_decoded_image_dump(decoded):
        return ''
    return decoded


def layout_notes_from_bytes(*, filename: str, content_type: str | None, contents: bytes) -> list[dict[str, object]]:
    suffix = Path(filename).suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    notes: list[dict[str, object]] = []
    if suffix == '.pdf' or mime == 'application/pdf' or is_pptx(filename, mime):
        slides = extract_presentation_slides(
            filename=filename, content_type=content_type, contents=contents
        )
        if slides:
            return layout_notes_from_slides(slides)
        if suffix != '.pdf' and mime != 'application/pdf':
            return notes
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(contents))
            page_count = len(reader.pages)
            for index, page in enumerate(reader.pages, start=1):
                text = normalize_text(page.extract_text() or '')
                notes.append({
                    'region': f'slide_or_page_{index}',
                    'page': index,
                    'page_count': page_count,
                    'char_count': len(text),
                    'excerpt': text[:8000],
                    'below_fold': index > 1,
                })
        except Exception as exc:
            logger.warning('marketing_pdf_layout_failed', error_type=type(exc).__name__)
    elif is_image_file(filename=filename, content_type=mime, contents=contents):
        notes.append({
            'region': 'slide_or_page_1',
            'page': 1,
            'page_count': 1,
            'char_count': 0,
            'excerpt': '',
            'image_count': 1,
            'font_note': 'screenshot',
            'label': 'screenshot',
            'media_type': sniff_image_media_type(contents, name=filename, declared=mime) or mime,
            'byte_size': len(contents),
        })
    return notes


def assert_safe_public_url(url: str) -> str:
    """Reject non-HTTPS and private/loopback targets (SSRF)."""
    raw = (url or '').strip()
    parsed = urlparse(raw)
    if parsed.scheme != 'https' or not parsed.hostname:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url must be a public https URL')
    host = parsed.hostname.lower().rstrip('.')
    if host in _PRIVATE_HOSTS or host.endswith('.local') or host.endswith('.internal'):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url host is not allowed')
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and (
        literal_ip.is_private
        or literal_ip.is_loopback
        or literal_ip.is_link_local
        or literal_ip.is_reserved
        or literal_ip.is_multicast
        or literal_ip.is_unspecified
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url host is not allowed')
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url host could not be resolved') from exc
    resolved_public = False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url resolves to a private address')
        resolved_public = True
    if not resolved_public:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url host could not be resolved')
    return raw


@dataclass
class FetchedPage:
    text: str
    notes: list[dict[str, object]] = field(default_factory=list)
    page_title: str = ''
    html_preview: str = ''


async def fetch_public_url_text(
    url: str,
    settings: Settings,
    *,
    capture: str = 'homepage',
) -> FetchedPage:
    safe_url = assert_safe_public_url(url)
    try:
        import httpx
    except ImportError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail='URL fetch is unavailable') from exc
    timeout = min(max(getattr(settings, 'database_connect_timeout', 10), 5), 20)
    max_bytes = settings.max_policy_upload_mb * 1024 * 1024
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    current = safe_url
    response = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(5):
            assert_safe_public_url(current)
            response = await client.get(current, headers=headers)
            if response.is_redirect:
                location = response.headers.get('location') or ''
                current = urljoin(current, location)
                continue
            break
        else:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='source_url redirected too many times')
    if response is None or response.status_code >= 400:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Unable to fetch source_url')
    body = response.content[:max_bytes]
    raw_html = body.decode('utf-8', errors='replace')
    if capture == 'blog':
        captured = capture_blog_from_html(raw_html, safe_url)
    else:
        captured = capture_homepage_from_html(raw_html, safe_url)
    captured.html_preview = build_renderable_page_html(
        raw_html,
        current or safe_url,
        prefer_article=capture == 'blog',
    )
    return captured


def capture_homepage_from_html(raw_html: str, source_url: str = '') -> FetchedPage:
    """Scrape visible homepage content into the firm-homepage review template."""
    soup = _homepage_soup(raw_html)
    if soup is None:
        text = _strip_html(raw_html)
        return FetchedPage(text=text[:MAX_BODY_CHARS], page_title='')
    for tag in soup(['script', 'style', 'noscript', 'template', 'svg']):
        tag.decompose()
    for tag in soup.find_all(['nav', 'iframe']):
        tag.decompose()
    page_title = _first_text(soup.find('title'), soup.find('h1'))
    og = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'og:title'})
    if og is not None:
        page_title = (og.get('content') or '').strip() or page_title
    page_title = clean_page_title(page_title, source_url=source_url)
    page_title = page_title or (urlparse(source_url).hostname or '').removeprefix('www.')
    hero = _hero_text(soup)
    badges = _badge_text(soup)
    testimonials = _testimonial_text(soup)
    mid_page = _mid_page_text(soup)
    footer_el = (
        soup.find('footer')
        or soup.find(attrs={'id': re.compile(r'footer', re.I)})
        or soup.find(class_=re.compile(r'footer', re.I))
    )
    footer = _visible_text(footer_el) if footer_el is not None else ''
    footer_note = _footer_font_note(footer_el) if footer_el is not None else 'below the fold, after the sitemap'
    full = _paragraph_text(soup.find('main') or soup.body or soup)
    if not footer:
        footer = _footer_fallback(full)
    if not hero:
        hero = _hero_fallback(soup, full, page_title)
    text = fill_homepage_template({
        'page_title': page_title,
        'hero': hero,
        'badges': badges,
        'testimonials': testimonials,
        'mid_page': mid_page,
        'footer': footer,
        'footer_note': footer_note,
        'full': full,
    })
    if homepage_body_is_thin(text, page_title):
        visible = _visible_text(soup.find('main') or soup.body or soup)
        if not homepage_body_is_thin(visible, page_title):
            hero = hero or visible[:500]
            full = visible
            text = fill_homepage_template({
                'page_title': page_title,
                'hero': hero,
                'badges': badges,
                'testimonials': testimonials,
                'mid_page': mid_page or visible,
                'footer': footer,
                'footer_note': footer_note,
                'full': full,
            })
    notes = _homepage_template_notes({
        'hero': hero,
        'badges': badges,
        'testimonials': testimonials,
        'mid_page': mid_page,
        'footer': footer,
        'footer_note': footer_note,
        'below_fold': footer_el is not None or bool(footer),
        'source_url': source_url,
        'page_title': page_title,
    })
    return FetchedPage(text=text[:MAX_BODY_CHARS], notes=notes, page_title=page_title[:240])


_BLOG_PROMO_SPLIT_RE = re.compile(r'this is exactly why we built|\bwe built\b', re.I)
_BLOG_OUTCOME_RE = re.compile(r'stayed on track|double digits|60/40', re.I)
_BLOG_CTA_RE = re.compile(r'book (a |my )?call|free strategy call|want to see how', re.I)
_BLOG_BUTTON_RE = re.compile(r'book my call|book a call|schedule a call', re.I)
_BLOG_BYLINE_RE = re.compile(
    r'^(by\s|written by |\d+\s*min read|published |insights\s*[>:/])',
    re.I,
)
_BLOG_DISCLOSURE_RE = re.compile(
    r'form adv|past performance does not|sec[- ]registered|not a guarantee of future',
    re.I,
)


def capture_blog_from_html(raw_html: str, source_url: str = '') -> FetchedPage:
    """Scrape an Insights/blog article into the CCO visual-pass template."""
    soup = _homepage_soup(raw_html)
    if soup is None:
        text = _strip_html(raw_html)
        return FetchedPage(text=text[:MAX_BODY_CHARS], page_title='')
    for tag in soup(['script', 'style', 'noscript', 'template', 'svg']):
        tag.decompose()
    for tag in soup.find_all(['nav', 'iframe']):
        tag.decompose()
    page_title = _first_text(soup.find('h1'), soup.find('title'))
    og = soup.find('meta', property='og:title') or soup.find('meta', attrs={'name': 'og:title'})
    if og is not None:
        page_title = (og.get('content') or '').strip() or page_title
    page_title = clean_page_title(page_title, source_url=source_url)
    page_title = page_title or (urlparse(source_url).hostname or '').removeprefix('www.')
    root = soup.find('article') or soup.find('main') or soup.body or soup
    paragraphs = _blog_paragraphs(root)
    educational: list[str] = []
    para_6 = ''
    para_7 = ''
    closing = ''
    seen_promo = False
    for para in paragraphs:
        if _BLOG_BUTTON_RE.search(para) and len(para) < 48:
            continue
        if _BLOG_BYLINE_RE.search(para) and len(para) < 120:
            continue
        if not seen_promo and _BLOG_PROMO_SPLIT_RE.search(para):
            seen_promo = True
            para_6 = para
            continue
        if seen_promo and not para_7 and _BLOG_OUTCOME_RE.search(para):
            para_7 = para
            continue
        if seen_promo and not closing and _BLOG_CTA_RE.search(para):
            closing = para
            continue
        if not seen_promo:
            educational.append(para)
    button = _blog_button_text(soup, paragraphs)
    footer_el = (
        soup.find('footer')
        or soup.find(attrs={'id': re.compile(r'footer', re.I)})
        or soup.find(class_=re.compile(r'footer', re.I))
    )
    footer = _visible_text(footer_el) if footer_el is not None else ''
    disclosures = footer if footer and _BLOG_DISCLOSURE_RE.search(footer) else ''
    if not disclosures:
        full = _visible_text(root)
        if _BLOG_DISCLOSURE_RE.search(full):
            disclosures = _footer_fallback(full)
    educational_text = '\n\n'.join(educational).strip()
    text = fill_blog_template({
        'page_title': page_title,
        'educational': educational_text,
        'para_6': para_6,
        'para_7': para_7,
        'closing': closing,
        'button': button,
        'disclosures': disclosures,
        'full': _paragraph_text(root),
    })
    notes = _blog_template_notes({
        'page_title': page_title,
        'educational': educational_text,
        'para_6': para_6,
        'para_7': para_7,
        'closing': closing,
        'button': button,
        'disclosures': disclosures,
    })
    return FetchedPage(text=text[:MAX_BODY_CHARS], notes=notes, page_title=page_title[:240])


def fill_blog_template(parts: dict[str, str]) -> str:
    """Map scraped blog regions onto the CCO visual-pass template."""
    blocks: list[str] = []
    title = (parts.get('page_title') or '').strip()
    if title:
        blocks.append(title)
    for key in ('educational', 'para_6', 'para_7', 'closing'):
        value = (parts.get(key) or '').strip()
        if value:
            blocks.append(value)
    button = (parts.get('button') or '').strip()
    if button:
        blocks.append(f'Button: {button}')
    disclosures = (parts.get('disclosures') or '').strip()
    if disclosures:
        blocks.append(disclosures)
    else:
        blocks.append('No disclosures anywhere on the page.')
    filled = '\n\n'.join(blocks)
    full = (parts.get('full') or '').strip()
    if full and full not in filled:
        filled = f'{filled}\n\nFull article text\n{full}'.strip() if filled else full
    return filled


def _blog_paragraphs(root: Any) -> list[str]:
    paragraphs: list[str] = []
    for node in root.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'blockquote', 'li', 'table']):
        if node.name == 'table' and node.find('p'):
            continue
        chunk = _visible_text(node)
        if len(chunk) >= 20:
            paragraphs.append(chunk)
    return list(dict.fromkeys(paragraphs))


def _blog_button_text(soup: Any, paragraphs: list[str]) -> str:
    for node in soup.find_all(['a', 'button']):
        text = _visible_text(node)
        if text and (_BLOG_BUTTON_RE.search(text) or _BLOG_CTA_RE.search(text)) and len(text) < 48:
            return text
    for para in paragraphs:
        if _BLOG_BUTTON_RE.search(para) and len(para) < 48:
            return para
    return ''


def _blog_template_notes(parts: dict[str, str]) -> list[dict[str, object]]:
    notes: list[dict[str, object]] = []
    mapping = (
        ('page_title', 'title', 'Insights blog headline'),
        ('educational', 'educational', 'paras 1–5 — educational, no firm mention'),
        ('para_6', 'para_6', 'promotional turn — offers advisory services'),
        ('para_7', 'para_7', '2022 client-outcome / 60/40 comparison'),
        ('closing', 'closing', 'closing CTA'),
        ('button', 'button', 'Book my call button'),
    )
    for key, region, font_note in mapping:
        excerpt = str(parts.get(key) or '').strip()
        if not excerpt:
            continue
        notes.append({
            'region': region,
            'font_note': font_note,
            'excerpt': excerpt[:_LAYOUT_EXCERPT_CHARS],
            'char_count': len(excerpt),
        })
    disclosures = str(parts.get('disclosures') or '').strip()
    notes.append({
        'region': 'disclosures',
        'font_note': 'no disclosures anywhere on the page' if not disclosures else 'page-level disclosures',
        'excerpt': disclosures[:_LAYOUT_EXCERPT_CHARS] if disclosures else 'No disclosures anywhere on the page.',
        'char_count': max(len(disclosures), 40),
    })
    return notes


def display_homepage_url(source_url: str) -> str:
    """Origin + path only — drop ads query strings from the visual-pass TITLE label."""
    raw = (source_url or '').strip()
    if not raw:
        return ''
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.split('?', 1)[0].split('#', 1)[0]
    path = parsed.path.rstrip('/')
    return f'{parsed.scheme}://{parsed.netloc}{path}'


def canonical_marketing_source_url(url: str) -> str:
    """Host + path identity so www/trailing-slash/https variants match the same page."""
    raw = (url or '').strip()
    if not raw:
        return ''
    parsed = urlparse(raw if '://' in raw else f'https://{raw}')
    host = (parsed.hostname or '').lower().removeprefix('www.')
    if not host:
        return display_homepage_url(raw).lower()
    path = (parsed.path or '').rstrip('/')
    return f'{host}{path}'


def clean_page_title(value: str, *, source_url: str = '') -> str:
    """Drop tracking URLs and query strings from the visual-pass TITLE."""
    text = str(value or '')
    if source_url:
        text = _strip_source_url(text, source_url)
    text = _HTTP_URL_RE.sub(' ', text)
    text = _WWW_URL_RE.sub(' ', text)
    text = _QUERY_TRACKING_RE.sub(' ', text)
    text = normalize_text(text).strip(' -|·•,;:/')
    return text


def _strip_source_url(text: str, source_url: str) -> str:
    candidates = [source_url.strip()]
    parsed = urlparse(source_url)
    if parsed.scheme and parsed.netloc:
        origin_path = f'{parsed.scheme}://{parsed.netloc}{parsed.path or "/"}'
        candidates.append(origin_path)
        candidates.append(origin_path.rstrip('/'))
    for candidate in dict.fromkeys(item for item in candidates if item):
        text = text.replace(candidate, ' ')
        text = text.replace(candidate.replace('&', '&amp;'), ' ')
    return text


def homepage_body_is_thin(text: str, page_title: str = '') -> bool:
    """True when the scrape kept a title/shell but not usable homepage copy."""
    body = (text or '').strip()
    title = (page_title or '').strip()
    if len(body) < 180:
        return True
    remainder = body
    if title:
        remainder = re.sub(re.escape(title), '', body, count=1, flags=re.I).strip()
    return len(remainder) < 120


def fill_homepage_template(parts: dict[str, str]) -> str:
    """Map scraped homepage regions onto the CCO visual-pass template."""
    blocks: list[str] = []
    title = (parts.get('page_title') or '').strip()
    if title:
        blocks.append(title)
    for key in ('hero', 'badges', 'testimonials', 'mid_page'):
        value = (parts.get(key) or '').strip()
        if value:
            blocks.append(value)
    footer = (parts.get('footer') or '').strip()
    if footer:
        note = (parts.get('footer_note') or 'below the fold, after the sitemap').strip()
        blocks.append(f'Footer — {note}: {footer}')
    filled = '\n\n'.join(blocks)
    full = (parts.get('full') or '').strip()
    if full and full not in filled:
        filled = f'{filled}\n\nFull page text\n{full}'.strip() if filled else full
    return filled


def _homepage_soup(raw_html: str) -> Any:
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(raw_html, 'html.parser')
    except Exception:
        return None


_PREVIEW_CSS = (
    'html,body{margin:0;background:#fff;color:#141414;}'
    'body{font-family:Georgia,"Times New Roman",serif;padding:1.5rem 2rem 3rem;line-height:1.45;}'
    'h1{font-size:2rem;font-weight:650;letter-spacing:-.02em;margin:0 0 .4rem;}'
    'h2{font-size:1.35rem;font-weight:650;margin:1.75rem 0 .45rem;}'
    'h3{font-size:1.1rem;font-weight:600;margin:1.1rem 0 .3rem;}'
    'article{max-width:42rem;}'
    'p,li{font-size:.95rem;margin:.35rem 0 .7rem;}'
    'img,picture,video{max-width:100%;height:auto;}'
    'nav,app-header,.header-navigation{display:none!important;}'
    'a.pvd-button-root,.pvd-button__text{display:inline-block;border:1px solid #141414;'
    'border-radius:4px;padding:.4rem .75rem;margin:.25rem .35rem .25rem 0;'
    'font-size:.9rem;text-decoration:none;color:#141414;}'
)


def _strip_active_html(soup: Any) -> None:
    for tag in soup(['script', 'noscript', 'iframe', 'object', 'embed', 'template', 'style']):
        tag.decompose()
    for link in list(soup.find_all('link')):
        link.decompose()
    for meta in list(soup.find_all('meta')):
        if str(meta.get('http-equiv') or '').lower() == 'refresh':
            meta.decompose()
    for tag in soup.find_all(True):
        attrs = getattr(tag, 'attrs', None) or {}
        for name in list(attrs):
            key = str(name).lower()
            value = str(attrs.get(name) or '')
            if key.startswith('on') or (key in {'href', 'src', 'action', 'xlink:href'} and _JS_URL_RE.match(value)):
                del tag.attrs[name]
            elif key == 'srcset' and 'javascript:' in value.lower():
                del tag.attrs[name]


def _page_content_root(soup: Any, *, prefer_article: bool = False) -> Any:
    articles = soup.find_all('article')
    article = max(articles, key=lambda node: len(node.get_text(' ', strip=True))) if articles else None
    main = soup.find('main') or soup.find(attrs={'role': 'main'})
    if prefer_article:
        return article or main or soup.body or soup
    return main or article or soup.body or soup


def build_renderable_page_html(
    raw_html: str,
    source_url: str = '',
    *,
    prefer_article: bool = False,
) -> str:
    """Keep the page's main copy and structure. Do not store the site's head/CSS shell."""
    from html import escape

    soup = _homepage_soup(raw_html)
    if soup is None:
        return (raw_html or '')[:_HTML_PREVIEW_CHARS]
    _strip_active_html(soup)
    for tag in soup.find_all('nav'):
        tag.decompose()
    root = _page_content_root(soup, prefer_article=prefer_article)
    footer = soup.find('footer')
    parts: list[str] = []
    if root is not None:
        parts.append(str(root))
    if footer is not None and root is not None and footer is not root and not prefer_article:
        if not (hasattr(root, 'find') and root.find('footer')):
            parts.append(str(footer))
    body_html = '\n'.join(part for part in parts if part).strip()
    if not body_html:
        body_html = f'<p>{escape(_visible_text(soup))}</p>' if _visible_text(soup) else ''
    title = ''
    if soup.title is not None:
        title = soup.title.get_text(' ', strip=True)
    base = f'<base href="{escape(source_url, quote=True)}">' if source_url else ''
    html = (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8">'
        f'{base}<title>{escape(title)}</title>'
        f'<style>{_PREVIEW_CSS}</style></head><body>{body_html}</body></html>'
    )
    return html[:_HTML_PREVIEW_CHARS]


def _visible_text(node: Any) -> str:
    if node is None:
        return ''
    if getattr(node, 'name', '') == 'meta':
        return normalize_text(str(node.get('content') or ''))
    return normalize_text(node.get_text(' ', strip=True))


def _class_blob(node: Any) -> str:
    classes = node.get('class') if hasattr(node, 'get') else None
    if isinstance(classes, list):
        return ' '.join(str(item) for item in classes)
    return str(classes or '')


def _is_leaf_copy_node(node: Any) -> bool:
    """True for a visible copy block, including card titles that are divs not paragraphs."""
    name = getattr(node, 'name', '') or ''
    if name in _BLOCK_TAGS:
        if name == 'li' and node.find(['p', 'li']):
            return False
        if name == 'p' and node.find('p'):
            return False
        return True
    if name not in {'div', 'span'}:
        return False
    if not _LEAF_COPY_RE.search(_class_blob(node)):
        return False
    if node.find(_BLOCK_TAGS):
        return False
    for child in node.find_all(['div', 'span']):
        if child is node:
            continue
        if _LEAF_COPY_RE.search(_class_blob(child)):
            return False
    return True


def _format_copy_block(node: Any, text: str) -> str:
    """Tag titles and CTAs so the review pane can restore heading vs body type."""
    name = getattr(node, 'name', '') or ''
    if name in {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}:
        return f'{"#" * int(name[1])} {text}'
    classes = _class_blob(node).lower()
    if re.search(r'button__text|btn[-_]?text|(?<![a-z])cta(?![a-z])', classes):
        return f'[{text}]'
    if re.search(r'subtitle|eyebrow|dek', classes):
        return f'#### {text}'
    if re.search(r'title|headline', classes):
        return f'### {text}'
    return text


def _paragraph_text(node: Any) -> str:
    """Keep block-level breaks so scraped pages are not one run-on line."""
    if node is None:
        return ''
    chunks: list[str] = []
    for child in node.find_all(True):
        if not _is_leaf_copy_node(child):
            continue
        text = normalize_text(child.get_text(' ', strip=True))
        if len(text) >= 8:
            chunks.append(_format_copy_block(child, text))
    if chunks:
        return '\n\n'.join(dict.fromkeys(chunks))
    return _visible_text(node)


def _first_text(*nodes: Any) -> str:
    for node in nodes:
        text = _visible_text(node)
        if text:
            return text
    return ''


def _hero_text(soup: Any) -> str:
    hero = soup.find(class_=re.compile(r'\bhero\b|banner|masthead', re.I)) or soup.find('header')
    h1 = (hero.find('h1') if hero is not None else None) or soup.find('h1')
    if h1 is None:
        return ''
    headline = _visible_text(h1)
    parts = [f'# {headline}' if headline else '']
    for sibling in h1.find_all_next(['p', 'h2', 'h3']):
        if sibling.name in {'h2', 'h3'}:
            break
        chunk = _visible_text(sibling)
        if chunk:
            parts.append(chunk)
        if len(' '.join(parts)) > 500:
            break
    return normalize_text('\n'.join(part for part in parts if part))


def _hero_fallback(soup: Any, full: str, page_title: str) -> str:
    desc = soup.find('meta', attrs={'name': 'description'}) or soup.find(
        'meta', property='og:description'
    )
    chunks = [page_title, _visible_text(desc)]
    root = soup.find('main') or soup.body or soup
    for paragraph in root.find_all('p')[:12]:
        chunk = _visible_text(paragraph)
        if len(chunk) >= 40:
            chunks.append(chunk)
        if sum(len(item) for item in chunks) > 500:
            break
    filled = normalize_text('\n'.join(item for item in chunks if item))
    if filled:
        return filled
    return normalize_text(full[:400])


def _footer_fallback(full: str) -> str:
    tail = (full or '')[-700:]
    if re.search(
        r'©|copyright|past performance|all rights reserved|form adv|not a guarantee',
        tail,
        re.I,
    ):
        return normalize_text(tail)
    return ''


def _badge_text(soup: Any) -> str:
    chunks: list[str] = []
    for node in soup.find_all(class_=re.compile(r'badge|award|rating|seal', re.I)):
        text = _visible_text(node)
        if text and _RATING_HINT_RE.search(text):
            chunks.append(text)
    if chunks:
        return normalize_text(' '.join(dict.fromkeys(chunks)))
    body = _visible_text(soup)
    match = _RATING_HINT_RE.search(body)
    if not match:
        return ''
    start = max(0, match.start() - 40)
    end = min(len(body), match.end() + 160)
    return normalize_text(body[start:end])


def _testimonial_text(soup: Any) -> str:
    chunks: list[str] = []
    for node in soup.find_all(['blockquote', 'q']) + soup.find_all(
        class_=re.compile(r'testimonial|review|quote', re.I)
    ):
        text = _visible_text(node)
        if text:
            chunks.append(text)
    if chunks:
        return normalize_text('\n'.join(dict.fromkeys(chunks)))
    quotes = re.findall(r'("|“|‘)([^"”’]{12,})("|”|’)', _visible_text(soup))
    if not quotes:
        return ''
    return normalize_text('\n'.join(f'“{item[1]}”' for item in quotes[:4]))


def _mid_page_text(soup: Any) -> str:
    """Reading-order mid-page copy. Do not clip to a single safety-claim window."""
    root = soup.find('main') or soup.body or soup
    text = _paragraph_text(root)
    if text:
        return text
    body = _visible_text(soup)
    match = _SAFETY_HINT_RE.search(body)
    if not match:
        return ''
    start = max(0, match.start() - 80)
    end = min(len(body), match.end() + 120)
    return normalize_text(body[start:end])


def _footer_font_note(node: Any) -> str:
    style = str((getattr(node, 'attrs', {}) or {}).get('style') or '')
    match = _FONT_SIZE_RE.search(style)
    if match:
        size = float(match.group(1))
        unit = match.group(2).lower()
        small = (unit == 'pt' and size <= 10) or (unit == 'px' and size <= 13)
        if small:
            return f'{size:g}{unit} grey, below the fold, after the sitemap'
    return 'below the fold, after the sitemap'


def _homepage_template_notes(parts: dict[str, object]) -> list[dict[str, object]]:
    notes: list[dict[str, object]] = []
    source_url = str(parts.get('source_url') or '').strip()
    title = clean_page_title(str(parts.get('page_title') or ''), source_url=source_url)
    if title:
        notes.append({
            'region': 'title',
            'font_note': display_homepage_url(source_url) or 'page title',
            'excerpt': title[:_LAYOUT_EXCERPT_CHARS],
            'char_count': len(title),
            'source_url': source_url,
        })
    mapping = (
        ('hero', 'full-width banner, 48pt headline', 'hero'),
        ('badges', 'three large gold seals, directly under hero', 'badge_row'),
        ('testimonials', 'rotating, 24pt type', 'testimonial'),
        ('mid_page', '16pt', 'mid_page'),
    )
    for key, font_note, region in mapping:
        excerpt = str(parts.get(key) or '').strip()
        if excerpt:
            notes.append({
                'region': region,
                'font_note': font_note,
                'excerpt': excerpt[:_LAYOUT_EXCERPT_CHARS],
                'char_count': len(excerpt),
            })
    footer = str(parts.get('footer') or '').strip()
    if footer:
        notes.append({
            'region': 'footer',
            'below_fold': bool(parts.get('below_fold')),
            'font_note': str(
                parts.get('footer_note') or '7pt grey text on white, below the fold, after the sitemap'
            ),
            'excerpt': footer[:_LAYOUT_EXCERPT_CHARS],
            'char_count': max(len(footer), 40),
        })
    return notes


def _strip_html(raw: str) -> str:
    without_scripts = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', raw)
    text = re.sub(r'(?is)<[^>]+>', ' ', without_scripts)
    return normalize_text(text)


def infer_format_from_inputs(format_hint: str | None, filename: str, content_type: str, source_url: str) -> str:
    if source_url and not format_hint:
        return 'webpage'
    resolved = normalize_format(format_hint, filename=filename, content_type=content_type)
    return resolved if resolved in FORMATS else 'text'
