"""Screenshot public homepage/blog URLs and place red-dot markers like pitch-deck slides."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Any

import structlog
from fastapi import HTTPException

from app.ai import ChatMessage, ImagePart, LLMProvider, TextPart

logger = structlog.get_logger(__name__)

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
MAX_PAGE_SLICES = 8
MAX_FULL_PAGE_HEIGHT = VIEWPORT_HEIGHT * MAX_PAGE_SLICES
PREVIEW_MAX_WIDTH = 1400
PREVIEW_JPEG_QUALITY = 72
CAPTURE_TIMEOUT_MS = 20_000
SETTLE_SECONDS = 1.5
PAGE_SCREENSHOT_FORMATS = frozenset({'webpage', 'blog'})

# Approximate placement when vision is unavailable. x/y are 0–1 within the slice.
_REGION_MARKERS: dict[str, tuple[float, float]] = {
    'title': (0.50, 0.10),
    'hero': (0.78, 0.28),
    'badge_row': (0.50, 0.52),
    'testimonial': (0.50, 0.76),
    'mid_page': (0.50, 0.40),
    'educational': (0.50, 0.36),
    'para_6': (0.72, 0.42),
    'para_7': (0.72, 0.58),
    'closing': (0.50, 0.72),
    'button': (0.50, 0.84),
    'footer': (0.50, 0.86),
    'disclosures': (0.50, 0.90),
    'subject': (0.92, 0.14),
    'body': (0.92, 0.42),
    'email_footer': (0.92, 0.88),
}

# 1-based slice, or -1 for the last captured slice (below the fold).
_REGION_SLICE: dict[str, int] = {
    'title': 1,
    'hero': 1,
    'badge_row': 1,
    'testimonial': 1,
    'educational': 1,
    'mid_page': 2,
    'para_6': 2,
    'para_7': 2,
    'closing': 2,
    'button': 2,
    'footer': -1,
    'disclosures': -1,
    'subject': 1,
    'body': 1,
    'email_footer': 1,
}


@dataclass
class PageScreenshot:
    page: int
    data: bytes
    width: int
    height: int
    source_url: str = ''
    font_note: str = ''
    image_storage_uri: str = ''


def has_page_screenshots(notes: list[dict[str, Any]] | None) -> bool:
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        region = str(note.get('region') or '')
        if region.startswith('slide_or_page_') and str(note.get('slide_image_path') or '').strip():
            return True
    return False


def screenshot_page_count(notes: list[dict[str, Any]] | None) -> int:
    pages = {
        int(note.get('page') or 0)
        for note in (notes or [])
        if isinstance(note, dict)
        and str(note.get('region') or '').startswith('slide_or_page_')
        and str(note.get('slide_image_path') or '').strip()
        and int(note.get('page') or 0) > 0
    }
    return max(pages) if pages else 0


def merge_screenshot_notes(
    existing: list[dict[str, Any]] | None,
    screenshot_notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep region template cards and replace any prior page-screenshot notes."""
    kept = [
        dict(note)
        for note in (existing or [])
        if isinstance(note, dict) and not str(note.get('region') or '').startswith('slide_or_page_')
    ]
    return list(screenshot_notes) + kept


def notes_from_screenshots(shots: list[PageScreenshot]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for shot in shots:
        if not shot.image_storage_uri:
            continue
        note: dict[str, Any] = {
            'region': f'slide_or_page_{shot.page}',
            'page': shot.page,
            'page_count': len(shots),
            'font_note': shot.font_note or ('above the fold' if shot.page == 1 else 'page screenshot'),
            'excerpt': shot.source_url,
            'char_count': len(shot.source_url),
            'image_count': 1,
            'below_fold': shot.page > 1,
            'slide_image_path': shot.image_storage_uri,
            'label': 'page',
        }
        notes.append(note)
    return notes


def slice_full_page_jpeg(
    data: bytes,
    *,
    source_url: str = '',
    slice_height: int = VIEWPORT_HEIGHT,
    max_slices: int = MAX_PAGE_SLICES,
) -> list[PageScreenshot]:
    """Split a full-page JPEG into viewport-height tiles so the UI can stack them like slides."""
    if not data:
        return []
    try:
        from PIL import Image
    except Exception as exc:
        logger.warning('marketing_page_screenshot_pil_missing', error_type=type(exc).__name__)
        return []
    try:
        with Image.open(BytesIO(data)) as raw:
            image = raw.convert('RGB')
    except Exception as exc:
        logger.warning('marketing_page_screenshot_open_failed', error_type=type(exc).__name__)
        return []
    if image.width > PREVIEW_MAX_WIDTH:
        ratio = PREVIEW_MAX_WIDTH / float(image.width)
        image = image.resize(
            (PREVIEW_MAX_WIDTH, max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    if image.height > MAX_FULL_PAGE_HEIGHT:
        image = image.crop((0, 0, image.width, MAX_FULL_PAGE_HEIGHT))
    height = image.height
    tile = max(int(slice_height), 200)
    shots: list[PageScreenshot] = []
    top = 0
    page = 1
    while top < height and page <= max_slices:
        bottom = min(top + tile, height)
        if bottom - top < 80 and shots:
            break
        crop = image.crop((0, top, image.width, bottom))
        encoded = _jpeg_bytes(crop)
        if encoded:
            shots.append(
                PageScreenshot(
                    page=page,
                    data=encoded,
                    width=crop.width,
                    height=crop.height,
                    source_url=source_url,
                    font_note='above the fold' if page == 1 else f'page {page}',
                )
            )
        top = bottom
        page += 1
    return shots


def attach_page_screenshot_paths(
    shots: list[PageScreenshot],
    *,
    org_id: str,
    content_id: str,
) -> None:
    if not org_id or not content_id:
        return
    from app.services.storage_service import storage_path, upload_file

    bucket = 'marketing-contents'
    for shot in shots:
        if shot.image_storage_uri or not shot.data:
            continue
        try:
            stored = upload_file(
                bucket,
                storage_path(org_id, 'marketing', content_id, f'slide-{shot.page}.jpg'),
                shot.data,
                'image/jpeg',
            )
            shot.image_storage_uri = stored.uri
        except Exception as exc:
            logger.warning(
                'marketing_page_screenshot_store_failed',
                page=shot.page,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )


def apply_region_markers(findings: list[dict[str, Any]], *, page_count: int) -> None:
    """Fallback marker placement from layout region when vision coords are missing."""
    pages = max(int(page_count or 0), 1)
    for index, item in enumerate(findings):
        if not isinstance(item, dict):
            continue
        region = str(item.get('region') or '').strip().lower()
        hint = _REGION_SLICE.get(region, 1)
        page = pages if hint < 0 else min(max(hint, 1), pages)
        default_x, default_y = _REGION_MARKERS.get(region, (0.90, min(0.14 + index * 0.12, 0.88)))
        if not item.get('slide'):
            item['slide'] = page
        if item.get('marker_x') is None:
            item['marker_x'] = default_x
        if item.get('marker_y') is None:
            item['marker_y'] = default_y


def apply_vision_marker_payload(findings: list[dict[str, Any]], payload: Any, *, page_count: int) -> None:
    markers = payload.get('markers') if isinstance(payload, dict) else payload
    if not isinstance(markers, list):
        return
    pages = max(int(page_count or 0), 1)
    by_id = {str(item.get('id') or ''): item for item in findings if isinstance(item, dict)}
    for row in markers:
        if not isinstance(row, dict):
            continue
        finding = by_id.get(str(row.get('id') or '').strip())
        if finding is None:
            continue
        page = _unit_page(row.get('page'), default=int(finding.get('slide') or 1), pages=pages)
        finding['slide'] = page
        finding['marker_x'] = _unit_coord(row.get('marker_x'), default=float(finding.get('marker_x') or 0.9))
        finding['marker_y'] = _unit_coord(row.get('marker_y'), default=float(finding.get('marker_y') or 0.2))


async def capture_public_page_jpeg(url: str) -> bytes | None:
    """Optional headless capture. Never installs browsers at runtime — that OOMs the API."""
    from app.services.marketing_ingest_service import assert_safe_public_url

    try:
        assert_safe_public_url(url)
    except HTTPException:
        return None
    logger.warning('marketing_page_screenshot_skipped', reason='disabled_in_api_process')
    return None


async def screenshot_public_page(
    url: str,
    *,
    org_id: str,
    content_id: str,
) -> list[PageScreenshot]:
    jpeg = await capture_public_page_jpeg(url)
    if not jpeg:
        return []
    shots = slice_full_page_jpeg(jpeg, source_url=url)
    attach_page_screenshot_paths(shots, org_id=org_id, content_id=content_id)
    logger.info('marketing_page_screenshots_captured', page_count=len(shots), url=url[:180])
    return shots


async def place_page_markers(
    findings: list[dict[str, Any]],
    shots: list[PageScreenshot],
    *,
    client: LLMProvider | None,
    format_name: str,
) -> None:
    page_count = len(shots) or 1
    apply_region_markers(findings, page_count=page_count)
    if not shots or client is None:
        return
    payload = await _vision_page_markers(
        findings,
        shots,
        client=client,
        format_name=format_name,
    )
    if payload is not None:
        apply_vision_marker_payload(findings, payload, page_count=page_count)


def _jpeg_bytes(image: Any) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format='JPEG', quality=PREVIEW_JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def _png_to_jpeg(png: bytes) -> bytes | None:
    if not png:
        return None
    try:
        from PIL import Image
    except Exception:
        return png if png.startswith(b'\xff\xd8\xff') else None
    try:
        with Image.open(BytesIO(png)) as raw:
            image = raw.convert('RGB')
    except Exception:
        return None
    if image.width > PREVIEW_MAX_WIDTH:
        ratio = PREVIEW_MAX_WIDTH / float(image.width)
        image = image.resize(
            (PREVIEW_MAX_WIDTH, max(1, int(image.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    return _jpeg_bytes(image)


def _unit_coord(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1:
        number = number / 100.0
    return min(max(number, 0.0), 1.0)


def _unit_page(value: Any, *, default: int, pages: int) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        page = default
    return min(max(page, 1), max(pages, 1))


def _marker_prompt(findings: list[dict[str, Any]], *, format_name: str, page_count: int) -> str:
    kind = 'firm homepage' if format_name == 'webpage' else 'blog article'
    lines: list[str] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get('id') or '').strip()
        excerpt = str(item.get('excerpt') or item.get('title') or '').replace('\n', ' ').strip()[:220]
        if not finding_id or not excerpt:
            continue
        region = str(item.get('region') or '').strip()
        marker = item.get('marker') or ''
        lines.append(f'- id={finding_id} C{marker} region={region} excerpt="{excerpt}"')
    catalog = '\n'.join(lines) or '- none'
    return (
        f'These {page_count} screenshot(s) are sequential viewport captures of a {kind}, '
        'top to bottom. Image 1 is above the fold. Place a marker on the screenshot where each '
        'finding excerpt appears. Return JSON only:\n'
        '{"markers":[{"id":"performance_gross_hero","page":1,"marker_x":0.82,"marker_y":0.18}]}\n'
        'page is 1-based matching the image order. marker_x and marker_y are 0-1 from the top-left '
        'of that page image, pointing at the cited sentence. Omit findings that are not visible.\n'
        f'Findings:\n{catalog}'
    )


async def _vision_page_markers(
    findings: list[dict[str, Any]],
    shots: list[PageScreenshot],
    *,
    client: LLMProvider,
    format_name: str,
) -> dict[str, Any] | None:
    from app.services.llm_json import parse_llm_json_dict
    from app.services.marketing_slide_service import ANTHROPIC_IMAGE_TYPES, sniff_image_media_type

    usable = [shot for shot in shots[:MAX_PAGE_SLICES] if shot.data]
    if not usable:
        return None
    prompt = _marker_prompt(findings, format_name=format_name, page_count=len(usable))
    content: list[TextPart | ImagePart] = []
    for shot in usable:
        media_type = sniff_image_media_type(shot.data, name=f'page-{shot.page}.jpg', declared='image/jpeg')
        if media_type not in ANTHROPIC_IMAGE_TYPES:
            continue
        content.append(ImagePart(media_type, base64.standard_b64encode(shot.data).decode('ascii')))
    if not content:
        return None
    content.append(TextPart(prompt))
    last_error = ''
    for attempt in range(2):
        try:
            response = await client.generate(
                [ChatMessage('user', content)],
                max_tokens=1_500,
                throttle='sonnet',
                trace_name='marketing.page_markers',
            )
            blob = response.text.strip()
            if not blob:
                continue
            parsed = parse_llm_json_dict(blob)
            if isinstance(parsed, dict):
                return parsed
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'[:300]
            logger.warning(
                'marketing_page_marker_vision_failed',
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
    logger.warning('marketing_page_marker_vision_empty', error=last_error)
    return None
