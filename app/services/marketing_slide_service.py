"""Split pitch decks into slides, then extract text and diagram payloads for embedding."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

import structlog

from app.ai import ChatMessage, ImagePart, LLMProvider, TextPart
from app.services.document_ingestion import normalize_text

logger = structlog.get_logger(__name__)

PPTX_SUFFIXES = frozenset({'.pptx'})
PPTX_MIME = 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
MAX_SLIDE_EXCERPT_CHARS = 8_000
MAX_DIAGRAM_IMAGES = 24
MAX_DIAGRAM_BYTES = 8_000_000
MAX_PREVIEW_SLIDES = 40
PREVIEW_MAX_WIDTH = 1400
PREVIEW_JPEG_QUALITY = 78
MIN_FULL_SLIDE_WIDTH = 640
MIN_FULL_SLIDE_HEIGHT = 360
MIN_FULL_SLIDE_BYTES = 2_048
MIN_NATIVE_TEXT_TO_SKIP_VISION = 160
ANTHROPIC_IMAGE_TYPES = frozenset({'image/jpeg', 'image/png', 'image/gif', 'image/webp'})
_PLACEHOLDER_DIAGRAM_RE = re.compile(
    r'^embedded diagram on slide \d+\s*\([^)]+\)\.?\s*$',
    re.I,
)
_SLIDE_HEADING_RE = re.compile(
    r'(?:^|\n)\s*(?:'
    r'slide\s+(\d+)(?:\s+of\s+(\d+))?[^\n]*'
    r'|appendix[^\n]{0,80}?slide\s+(\d+)'
    r')',
    re.I,
)
_A_NS = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
_P_NS = '{http://schemas.openxmlformats.org/presentationml/2006/main}'
_R_NS = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'


def _slide_title_line(text: str) -> str:
    for line in (text or '').splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r'slide\s+\d+(?:\s+of\s+\d+)?\b', stripped, re.I):
            continue
        if _is_placeholder_diagram_text(stripped):
            continue
        return stripped[:120]
    return ''


def _is_placeholder_diagram_text(value: str) -> bool:
    return bool(_PLACEHOLDER_DIAGRAM_RE.match((value or '').strip()))


def sniff_image_media_type(data: bytes, *, name: str = '', declared: str = '') -> str:
    """Prefer file signatures so JPEG-in-.png screenshot uploads are sent as JPEG."""
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'GIF87a') or data.startswith(b'GIF89a'):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    mime = (declared or '').split(';')[0].strip().lower()
    if mime in ANTHROPIC_IMAGE_TYPES:
        return mime
    suffix = Path(name or '').suffix.lower()
    return {
        '.png': 'image/png',
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.webp': 'image/webp',
        '.gif': 'image/gif',
    }.get(suffix, '')


def is_image_file(*, filename: str = '', content_type: str = '', contents: bytes = b'') -> bool:
    """True for PNG/JPEG/WebP/GIF even when the upload is labeled text/plain."""
    suffix = Path(filename or '').suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    if mime.startswith('image/') or suffix in {'.png', '.jpg', '.jpeg', '.webp', '.gif'}:
        return True
    return sniff_image_media_type(contents, name=filename, declared=mime) in ANTHROPIC_IMAGE_TYPES


@dataclass
class SlideImage:
    media_type: str
    data: bytes
    name: str = ''
    sha256: str = ''

    def __post_init__(self) -> None:
        if not self.sha256 and self.data:
            self.sha256 = hashlib.sha256(self.data).hexdigest()


@dataclass
class SlidePayload:
    page: int
    text: str
    page_count: int = 1
    images: list[SlideImage] = field(default_factory=list)
    diagram_captions: list[str] = field(default_factory=list)
    vision_findings: list[dict[str, Any]] = field(default_factory=list)
    image_storage_uri: str = ''
    preview_image: SlideImage | None = None
    label: str = 'slide'

    @property
    def diagram_text(self) -> str:
        parts = [
            item.strip()
            for item in self.diagram_captions
            if item and item.strip() and not _is_placeholder_diagram_text(item)
        ]
        return '\n'.join(parts)

    def embed_text(self) -> str:
        chunks = [f'SLIDE {self.page}', self.text.strip()]
        diagram = self.diagram_text
        if diagram:
            chunks.append('DIAGRAM')
            chunks.append(diagram)
        return '\n'.join(chunk for chunk in chunks if chunk).strip()

    def as_layout_notes(self) -> list[dict[str, Any]]:
        visible = (self.text or '').strip() or (self.diagram_text or '').strip()
        font = _slide_title_line(visible)
        if not font and self.images and not (self.text or '').strip():
            font = 'screenshot'
        notes: list[dict[str, Any]] = [{
            'region': f'slide_or_page_{self.page}',
            'page': self.page,
            'page_count': self.page_count,
            'char_count': len(visible),
            'excerpt': visible[:MAX_SLIDE_EXCERPT_CHARS],
            'image_count': len(self.images),
            'below_fold': False,
            'font_note': font,
            'label': self.label,
        }]
        if self.image_storage_uri:
            notes[0]['slide_image_path'] = self.image_storage_uri
        diagram = self.diagram_text
        if diagram:
            notes.append({
                'region': 'diagram',
                'page': self.page,
                'font_note': 'embedded chart / image converted for vector comparison',
                'excerpt': diagram[:MAX_SLIDE_EXCERPT_CHARS],
                'char_count': len(diagram),
                'image_count': len(self.images),
            })
        return notes


def is_pptx(filename: str = '', content_type: str = '') -> bool:
    suffix = Path(filename or '').suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    return suffix in PPTX_SUFFIXES or mime == PPTX_MIME


def parse_slides_from_text(text: str) -> list[SlidePayload]:
    """Split pasted pitch-deck copy on Slide N / appendix markers."""
    blob = text or ''
    matches = list(_SLIDE_HEADING_RE.finditer(blob))
    if not matches:
        stripped = blob.strip()
        return [SlidePayload(page=1, text=stripped, page_count=1)] if stripped else []

    slides: list[SlidePayload] = []
    prefix = blob[: matches[0].start()].strip()
    declared_count = 0
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(blob)
        chunk = blob[start:end].strip()
        if index == 0 and prefix:
            chunk = f'{prefix}\n\n{chunk}'.strip()
        page = int(match.group(1) or match.group(3) or (index + 1))
        if match.group(2):
            declared_count = max(declared_count, int(match.group(2)))
        label = 'appendix' if 'appendix' in match.group(0).lower() else 'slide'
        slides.append(SlidePayload(page=page, text=chunk, label=label))
    page_count = max(declared_count, max((item.page for item in slides), default=1))
    for item in slides:
        item.page_count = page_count
    return slides


def slides_from_layout_notes(
    layout_notes: list[dict[str, Any]] | None,
    *,
    body_text: str = '',
) -> list[SlidePayload]:
    """Rebuild slide payloads from stored visual-pass notes, falling back to pasted copy."""
    grouped: dict[int, SlidePayload] = {}
    has_page_splits = False
    for note in layout_notes or []:
        if not isinstance(note, dict):
            continue
        region = str(note.get('region') or '')
        if region.startswith('slide_or_page_'):
            has_page_splits = True
            break
    if not has_page_splits:
        return parse_slides_from_text(body_text)

    for note in layout_notes or []:
        if not isinstance(note, dict):
            continue
        region = str(note.get('region') or '')
        page = int(note.get('page') or 0)
        if region.startswith('slide_or_page_'):
            try:
                page = int(region.rsplit('_', 1)[-1])
            except ValueError:
                page = page or 1
        if page <= 0:
            continue
        slide = grouped.get(page) or SlidePayload(
            page=page,
            text='',
            page_count=int(note.get('page_count') or page),
            label=str(note.get('label') or 'slide'),
        )
        excerpt = str(note.get('excerpt') or '').strip()
        if _is_placeholder_diagram_text(excerpt):
            excerpt = ''
        if region.startswith('slide_or_page_'):
            if excerpt:
                slide.text = excerpt if len(excerpt) >= len(slide.text) else slide.text
            image_path = str(note.get('slide_image_path') or '').strip()
            if image_path:
                slide.image_storage_uri = image_path
        elif region == 'diagram' and excerpt:
            if excerpt not in slide.diagram_captions:
                slide.diagram_captions.append(excerpt)
        grouped[page] = slide
    slides = [grouped[key] for key in sorted(grouped)]
    if slides:
        page_count = max(item.page_count for item in slides)
        for item in slides:
            item.page_count = max(item.page_count, page_count)
        return slides
    return parse_slides_from_text(body_text)


def extract_presentation_slides(
    *,
    filename: str,
    content_type: str | None,
    contents: bytes,
) -> list[SlidePayload]:
    suffix = Path(filename or '').suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    if is_pptx(filename, mime):
        return _extract_pptx_slides(contents)
    if suffix == '.pdf' or mime == 'application/pdf':
        return _extract_pdf_slides(contents)
    return []


def slides_from_screenshot_file(
    *,
    filename: str,
    content_type: str | None,
    contents: bytes,
) -> list[SlidePayload]:
    """Treat a PNG/JPEG/WebP capture as one slide, or split a PDF/PPTX as usual."""
    if not contents:
        return []
    suffix = Path(filename or '').suffix.lower()
    mime = (content_type or '').split(';')[0].strip().lower()
    sniffed = sniff_image_media_type(contents, name=filename or '', declared=mime)
    if sniffed in ANTHROPIC_IMAGE_TYPES or mime.startswith('image/') or suffix in {
        '.png',
        '.jpg',
        '.jpeg',
        '.webp',
        '.gif',
    }:
        if sniffed in ANTHROPIC_IMAGE_TYPES:
            media_type = sniffed
        elif mime.startswith('image/'):
            media_type = mime
        else:
            media_type = 'image/png'
        image = SlideImage(
            media_type=media_type,
            data=contents,
            name=filename or f'screenshot.{media_type.split("/")[-1]}',
        )
        return [SlidePayload(
            page=1,
            text='',
            page_count=1,
            images=[image],
            preview_image=image,
            label='screenshot',
        )]
    return extract_presentation_slides(
        filename=filename,
        content_type=content_type,
        contents=contents,
    )


def layout_notes_from_slides(slides: list[SlidePayload]) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for slide in slides:
        notes.extend(slide.as_layout_notes())
    return dedupe_presentation_layout_notes(notes)


def dedupe_presentation_layout_notes(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one card per slide, in deck order. Re-ingest used to append a second copy."""
    slides: dict[int, dict[str, Any]] = {}
    rest: list[dict[str, Any]] = []
    seen_rest: set[tuple[str, str, str]] = set()
    for note in notes:
        if not isinstance(note, dict):
            continue
        region = str(note.get('region') or '')
        if region.startswith('slide_or_page_'):
            try:
                page = int(note.get('page') or region.rsplit('_', 1)[-1])
            except (TypeError, ValueError):
                page = 0
            if page <= 0:
                rest.append(note)
                continue
            current = dict(note)
            current['page'] = page
            current['region'] = f'slide_or_page_{page}'
            previous = slides.get(page)
            if previous is None or len(str(current.get('excerpt') or '')) > len(
                str(previous.get('excerpt') or '')
            ):
                slides[page] = current
            continue
        key = (region, str(note.get('page') or ''), str(note.get('excerpt') or '')[:120])
        if key in seen_rest:
            continue
        seen_rest.add(key)
        rest.append(note)
    return [slides[page] for page in sorted(slides)] + rest


def presentation_source_filename(*, storage_path: str = '', content_type: str = '') -> str:
    """Prefer the stored filename so screenshot PPTX is not parsed as PDF."""
    name = Path((storage_path or '').split('?')[0]).name
    mime = (content_type or '').split(';')[0].strip().lower()
    if name.lower().endswith('.pptx') or is_pptx(name, mime):
        return name if name.lower().endswith('.pptx') else 'deck.pptx'
    if 'powerpoint' in mime or 'presentationml' in mime:
        return 'deck.pptx'
    if name.lower().endswith('.pdf') or mime == 'application/pdf':
        return name if name.lower().endswith('.pdf') else 'deck.pdf'
    return name or 'deck.pdf'


def joined_slide_text(slides: list[SlidePayload]) -> str:
    from app.services.marketing_ingest_service import is_decoded_image_dump

    parts: list[str] = []
    screenshot_only = len(slides) == 1 and (slides[0].label or '') == 'screenshot'
    for item in slides:
        body = (item.text or '').strip() or (item.diagram_text or '').strip()
        if not body or is_decoded_image_dump(body):
            continue
        if screenshot_only:
            parts.append(body)
            continue
        parts.append(f'Slide {item.page} of {item.page_count}\n{body}'.strip())
    return '\n\n'.join(parts)


def slide_embed_corpus(slides: list[SlidePayload]) -> list[tuple[int, str]]:
    """Text case + diagram case payload for each slide."""
    corpus: list[tuple[int, str]] = []
    for slide in slides:
        blob = slide.embed_text()
        if blob:
            corpus.append((slide.page, blob))
    return corpus


def _extract_pdf_slides(contents: bytes) -> list[SlidePayload]:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        logger.warning('marketing_pdf_import_failed', error_type=type(exc).__name__)
        return []
    try:
        reader = PdfReader(BytesIO(contents))
    except Exception as exc:
        logger.warning('marketing_pdf_read_failed', error_type=type(exc).__name__)
        return []
    page_count = len(reader.pages)
    slides: list[SlidePayload] = []
    image_budget = 0
    for index, page in enumerate(reader.pages, start=1):
        text = normalize_text(page.extract_text() or '')
        images: list[SlideImage] = []
        for image in _pdf_page_images(page):
            if image_budget >= MAX_DIAGRAM_IMAGES:
                break
            if not image.data or len(image.data) > MAX_DIAGRAM_BYTES:
                continue
            images.append(image)
            image_budget += 1
        slides.append(SlidePayload(page=index, text=text, page_count=page_count, images=images))
    return slides


def _pdf_page_images(page: Any) -> list[SlideImage]:
    images: list[SlideImage] = []
    raw_images = getattr(page, 'images', None)
    if not raw_images:
        return images
    try:
        iterator = list(raw_images)
    except Exception:
        return images
    for item in iterator:
        try:
            data = bytes(getattr(item, 'data', b'') or b'')
        except Exception:
            continue
        if not data:
            continue
        name = str(getattr(item, 'name', '') or 'image')
        images.append(SlideImage(
            media_type=sniff_image_media_type(data, name=name),
            data=data,
            name=name,
        ))
    return images


def _extract_pptx_slides(contents: bytes) -> list[SlidePayload]:
    try:
        archive = ZipFile(BytesIO(contents))
    except Exception as exc:
        logger.warning('marketing_pptx_read_failed', error_type=type(exc).__name__)
        return []
    slide_names = _pptx_slide_names_in_order(archive)
    page_count = len(slide_names)
    slides: list[SlidePayload] = []
    image_budget = 0
    for index, name in enumerate(slide_names, start=1):
        try:
            xml = archive.read(name)
            text = _pptx_slide_text(xml)
            images = _pptx_slide_images(archive, name, remaining=MAX_DIAGRAM_IMAGES - image_budget)
            image_budget += len(images)
            slides.append(SlidePayload(page=index, text=text, page_count=page_count, images=images))
        except Exception as exc:
            logger.warning('marketing_pptx_slide_failed', slide=name, error_type=type(exc).__name__)
            slides.append(SlidePayload(page=index, text='', page_count=page_count))
    return slides


def _pptx_slide_sort_key(name: str) -> int:
    match = re.search(r'slide(\d+)\.xml$', name)
    return int(match.group(1)) if match else 0


def _pptx_slide_names_in_order(archive: ZipFile) -> list[str]:
    present = [
        name for name in archive.namelist()
        if re.fullmatch(r'ppt/slides/slide\d+\.xml', name)
    ]
    ordered = _pptx_presentation_slide_targets(archive)
    if ordered:
        seen: set[str] = set()
        names: list[str] = []
        for target in ordered:
            if target in present and target not in seen:
                names.append(target)
                seen.add(target)
        for name in sorted(present, key=_pptx_slide_sort_key):
            if name not in seen:
                names.append(name)
        return names
    return sorted(present, key=_pptx_slide_sort_key)


def _pptx_presentation_slide_targets(archive: ZipFile) -> list[str]:
    try:
        presentation = ElementTree.fromstring(archive.read('ppt/presentation.xml'))
        rels = ElementTree.fromstring(archive.read('ppt/_rels/presentation.xml.rels'))
    except Exception:
        return []
    rel_map: dict[str, str] = {}
    for rel in rels:
        rel_id = str(rel.attrib.get('Id') or rel.attrib.get(f'{_R_NS}Id') or '')
        target = str(rel.attrib.get('Target') or '')
        if not rel_id or 'slide' not in target.lower():
            continue
        path = target.lstrip('/')
        if not path.startswith('ppt/'):
            path = f'ppt/{path}'
        rel_map[rel_id] = path.replace('\\', '/')
    ordered: list[str] = []
    for slide_id in presentation.iter(f'{_P_NS}sldId'):
        rel_id = str(slide_id.attrib.get(f'{_R_NS}id') or '')
        if not rel_id:
            for key, value in slide_id.attrib.items():
                if str(key).endswith('id') and str(value).startswith('rId'):
                    rel_id = str(value)
                    break
        slide_path = rel_map.get(rel_id)
        if slide_path:
            ordered.append(slide_path)
    return ordered


def _pptx_slide_text(xml: bytes) -> str:
    root = ElementTree.fromstring(xml)
    parts = [node.text.strip() for node in root.iter(f'{_A_NS}t') if node.text and node.text.strip()]
    return normalize_text('\n'.join(parts))


def _pptx_slide_images(archive: ZipFile, slide_name: str, *, remaining: int) -> list[SlideImage]:
    if remaining <= 0:
        return []
    rels_name = slide_name.replace('ppt/slides/', 'ppt/slides/_rels/') + '.rels'
    if rels_name not in archive.namelist():
        return []
    try:
        rels_root = ElementTree.fromstring(archive.read(rels_name))
    except Exception:
        return []
    images: list[SlideImage] = []
    for rel in rels_root:
        target = str(rel.attrib.get('Target') or '')
        rel_type = str(rel.attrib.get('Type') or rel.attrib.get(f'{_R_NS}Type') or '')
        if 'image' not in rel_type.lower() and not re.search(r'\.(png|jpe?g|webp|gif)$', target, re.I):
            continue
        media_path = _pptx_resolve(slide_name, target)
        if media_path not in archive.namelist():
            continue
        try:
            data = archive.read(media_path)
        except Exception:
            continue
        if not data or len(data) > MAX_DIAGRAM_BYTES:
            continue
        media = sniff_image_media_type(data, name=media_path)
        if not media:
            continue
        images.append(SlideImage(media_type=media, data=data, name=Path(media_path).name))
        if len(images) >= remaining:
            break
    return images


def _pptx_resolve(slide_name: str, target: str) -> str:
    if target.startswith('/'):
        return target.lstrip('/')
    if target.startswith('../'):
        return 'ppt/' + target.replace('../', '', 1)
    folder = str(Path(slide_name).parent)
    return str(Path(folder, target))


def slide_needs_screenshot_vision(slide: SlidePayload) -> bool:
    """True when native PPTX/PDF text is too thin to review without reading the pixels."""
    native = (slide.text or '').strip()
    if len(native) < MIN_NATIVE_TEXT_TO_SKIP_VISION:
        return True
    return False


def vision_images_for_slide(slide: SlidePayload) -> list[SlideImage]:
    """Prefer a full-slide screenshot over logos so later screenshot slides still get OCR."""
    if not slide_needs_screenshot_vision(slide):
        return []
    full = slide_preview_image(slide)
    if full is not None and full.data:
        return [full]
    return [item for item in slide.images if item.data][:2]


def _is_full_slide_vision_image(slide: SlidePayload, image: SlideImage) -> bool:
    if image is slide.preview_image:
        return True
    return is_full_slide_image(image)


async def caption_slide_diagrams(
    slides: list[SlidePayload],
    *,
    client: LLMProvider | None,
    max_images: int = MAX_DIAGRAM_IMAGES,
) -> list[SlidePayload]:
    """OCR screenshot/chart images and extract advertising findings from the pixels."""
    if client is None:
        logger.warning('marketing_diagram_caption_skipped', reason='llm_provider_missing')
        return slides
    remaining = max(max_images, 0)
    pending = [slide for slide in slides if vision_images_for_slide(slide)]
    pending.sort(key=lambda slide: (0 if not (slide.text or '').strip() else 1, slide.page))
    for slide in pending:
        if remaining <= 0:
            logger.warning(
                'marketing_slide_vision_budget_exhausted',
                remaining_slides=sum(1 for item in pending if item.page >= slide.page),
            )
            break
        for image in vision_images_for_slide(slide):
            if remaining <= 0:
                break
            caption = await _caption_slide_image(image, client=client, page=slide.page)
            if caption:
                apply_slide_vision_response(
                    slide,
                    caption,
                    as_diagram=not _is_full_slide_vision_image(slide, image),
                )
            remaining -= 1
        if not (slide.text or '').strip() and slide.diagram_captions:
            from app.services.marketing_ingest_service import is_decoded_image_dump

            captions = [
                item for item in slide.diagram_captions
                if item and item.strip() and not is_decoded_image_dump(item)
            ]
            if captions:
                slide.text = '\n\n'.join(captions)
    return slides


SLIDE_VISION_PROMPT = (
    'Read this screenshot of firm marketing (webpage, blog, email, social post, or slide). '
    'Transcribe every visible word in reading order: headlines, body, badges, ratings, '
    'buttons, and footer. Copy numbers and ratings exactly. Do not invent text. '
    'Then review the visible copy against SEC Rule 206(4)-1 and FINRA 2210. '
    'Return JSON only:\n'
    '{'
    '"transcription": "full readable text in reading order",'
    '"findings": [{'
    '"title": "short gap name",'
    '"excerpt": "the exact quoted sentence or headline that caused this finding",'
    '"rule": "206(4)-1",'
    '"why": "why those exact words are an advertising issue",'
    '"recommended_option": "redline",'
    '"draft_text": "compliant replacement the CCO can accept",'
    '"anchor": "headline",'
    '"marker_x": 0.86,'
    '"marker_y": 0.18'
    '}]}'
    '\ntranscription is required. excerpt MUST be verbatim from the screenshot. '
    'marker_x and marker_y are 0-1 from the top-left of the image. '
    'If there is no advertising issue, findings may be [].'
)


def apply_slide_vision_response(
    slide: SlidePayload,
    raw: str,
    *,
    as_diagram: bool = True,
) -> str:
    """Attach OCR text and any vision findings. Plain-text captions still count as OCR."""
    from app.services.llm_json import parse_llm_json_dict

    blob = (raw or '').strip()
    if not blob:
        return ''
    from app.services.marketing_ingest_service import is_decoded_image_dump

    if is_decoded_image_dump(blob):
        return ''
    parsed: dict[str, Any] | None = None
    try:
        parsed = parse_llm_json_dict(blob)
    except (ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        transcription = str(parsed.get('transcription') or '').strip()
        raw_findings = parsed.get('findings')
        findings = raw_findings if isinstance(raw_findings, list) else []
        for item in findings:
            if isinstance(item, dict) and (item.get('excerpt') or item.get('title') or item.get('why')):
                slide.vision_findings.append(item)
        if transcription:
            if is_decoded_image_dump(transcription):
                transcription = ''
            else:
                _attach_slide_transcription(slide, transcription, as_diagram=as_diagram)
                return transcription
        if findings:
            return blob
    _attach_slide_transcription(slide, blob, as_diagram=as_diagram)
    return blob


def _attach_slide_transcription(slide: SlidePayload, text: str, *, as_diagram: bool) -> None:
    blob = (text or '').strip()
    if not blob:
        return
    if as_diagram and blob not in slide.diagram_captions:
        slide.diagram_captions.append(blob)
    existing = (slide.text or '').strip()
    if not existing:
        slide.text = blob
    elif blob not in existing:
        slide.text = f'{existing}\n\n{blob}'


def findings_from_slide_vision(slides: list[SlidePayload]) -> list[dict[str, Any]]:
    """Turn per-slide vision JSON into review findings that can drive a redline."""
    findings: list[dict[str, Any]] = []
    for slide in slides:
        for index, item in enumerate(slide.vision_findings, start=1):
            excerpt = str(
                item.get('excerpt') or item.get('cause') or item.get('quote') or ''
            ).strip()
            why = str(item.get('why') or '').strip()
            if not excerpt and not why:
                continue
            option = str(item.get('recommended_option') or 'redline').strip().lower()
            if option not in {
                'redline', 'add_disclosure', 'pull', 'do_nothing', 'escalate', 'held',
            }:
                option = 'redline'
            if option == 'do_nothing' and not why:
                continue
            findings.append({
                'id': str(item.get('id') or f'vision_slide_{slide.page}_{index}'),
                'title': str(item.get('title') or excerpt)[:180],
                'excerpt': excerpt[:800],
                'rule': str(item.get('rule') or '206(4)-1'),
                'why': why or f'Slide {slide.page} contains an advertising claim that needs a CCO edit.',
                'confidence': str(item.get('confidence') or 'certain'),
                'options': ['redline', 'add_disclosure', 'pull', 'do_nothing'],
                'recommended_option': option if option != 'held' else 'redline',
                'status': 'open',
                'draft_text': str(item.get('draft_text') or '').strip(),
                'slide': slide.page,
                'region': str(item.get('region') or 'slide_copy'),
                'anchor': str(item.get('anchor') or 'slide copy'),
                'marker_x': _unit_coord(item.get('marker_x'), default=0.92),
                'marker_y': _unit_coord(item.get('marker_y'), default=min(0.12 + (index - 1) * 0.14, 0.88)),
            })
    return findings


def bind_findings_to_slides(findings: list[dict[str, Any]], slides: list[SlidePayload]) -> None:
    """Fill missing slide numbers when a finding excerpt appears in OCR text."""
    for item in findings:
        if not isinstance(item, dict) or item.get('slide'):
            continue
        excerpt = str(item.get('excerpt') or '').strip().lower()
        if len(excerpt) < 8:
            continue
        needle = excerpt[:80]
        for slide in slides:
            haystack = f'{slide.text}\n{slide.diagram_text}'.lower()
            if needle in haystack:
                item['slide'] = slide.page
                break


def _unit_coord(value: Any, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number > 1:
        number = number / 100.0
    return min(max(number, 0.0), 1.0)


def _image_dimensions(image: SlideImage) -> tuple[int, int] | None:
    if not image.data:
        return None
    try:
        from PIL import Image

        with Image.open(BytesIO(image.data)) as frame:
            return int(frame.size[0]), int(frame.size[1])
    except Exception:
        return None


def is_full_slide_image(image: SlideImage) -> bool:
    """True when an embedded picture is a slide screenshot, not a logo or icon."""
    if not image.data or len(image.data) < MIN_FULL_SLIDE_BYTES:
        return False
    size = _image_dimensions(image)
    if size is None:
        return len(image.data) >= 80_000
    width, height = size
    return width >= MIN_FULL_SLIDE_WIDTH and height >= MIN_FULL_SLIDE_HEIGHT


def slide_preview_image(slide: SlidePayload) -> SlideImage | None:
    if slide.preview_image and slide.preview_image.data:
        return slide.preview_image
    candidates = [item for item in slide.images if is_full_slide_image(item)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(item.data))


def _soffice_bin() -> str | None:
    found = shutil.which('soffice') or shutil.which('libreoffice')
    if found:
        return found
    mac = Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    return str(mac) if mac.exists() else None


def _pptx_to_pdf_bytes(contents: bytes) -> bytes | None:
    soffice = _soffice_bin()
    if not soffice:
        logger.warning('marketing_slide_preview_skipped', reason='libreoffice_missing')
        return None
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = work / 'deck.pptx'
        source.write_bytes(contents)
        env = os.environ.copy()
        env['HOME'] = str(work)
        env.setdefault('SAL_USE_VCLPLUGIN', 'svp')
        try:
            completed = subprocess.run(
                [
                    soffice,
                    '--headless',
                    '--norestore',
                    '--nolockcheck',
                    '--nologo',
                    '--convert-to',
                    'pdf',
                    '--outdir',
                    str(work),
                    str(source),
                ],
                check=False,
                capture_output=True,
                timeout=180,
                env=env,
            )
        except Exception as exc:
            logger.warning(
                'marketing_slide_preview_convert_failed',
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            return None
        pdf = work / 'deck.pdf'
        if not pdf.exists() or not pdf.stat().st_size:
            logger.warning(
                'marketing_slide_preview_convert_empty',
                returncode=completed.returncode,
                stderr=(completed.stderr or b'')[:240].decode('utf-8', 'ignore'),
            )
            return None
        return pdf.read_bytes()


def presentation_pdf_bytes(contents: bytes, filename: str) -> bytes | None:
    suffix = Path(filename or '').suffix.lower()
    if suffix == '.pdf' or sniff_pdf(contents):
        return contents
    if suffix in PPTX_SUFFIXES or is_pptx(filename, ''):
        return _pptx_to_pdf_bytes(contents)
    return None


def sniff_pdf(contents: bytes) -> bool:
    return contents[:5] == b'%PDF-'


def file_slides_for_marketing_review(
    *,
    filename: str,
    content_type: str | None,
    contents: bytes,
) -> list[SlidePayload]:
    """Turn any uploaded screenshot, PDF, or deck into slides for vision OCR."""
    if not contents:
        return []
    mime = (content_type or '').split(';')[0].strip().lower()
    if is_image_file(filename=filename, content_type=mime, contents=contents):
        return slides_from_screenshot_file(
            filename=filename,
            content_type=content_type,
            contents=contents,
        )
    suffix = Path(filename or '').suffix.lower()
    if is_pptx(filename, mime) or suffix == '.pdf' or mime == 'application/pdf' or sniff_pdf(contents):
        return extract_presentation_slides(
            filename=filename,
            content_type=content_type,
            contents=contents,
        )
    return []


def _pdf_page_jpegs(pdf_bytes: bytes, *, pages: int) -> dict[int, bytes]:
    if not pdf_bytes or pages <= 0:
        return {}
    try:
        import pypdfium2 as pdfium
        from PIL import Image
    except Exception as exc:
        logger.warning('marketing_slide_preview_raster_import_failed', error_type=type(exc).__name__)
        return {}
    document = None
    rendered: dict[int, bytes] = {}
    try:
        document = pdfium.PdfDocument(pdf_bytes)
        count = min(len(document), pages, MAX_PREVIEW_SLIDES)
        for index in range(count):
            page = document[index]
            width, height = page.get_size()
            scale = min(PREVIEW_MAX_WIDTH / max(float(width), 1.0), 2.0)
            bitmap = page.render(scale=max(scale, 0.8))
            image = bitmap.to_pil()
            if image.mode != 'RGB':
                image = image.convert('RGB')
            if image.width > PREVIEW_MAX_WIDTH:
                ratio = PREVIEW_MAX_WIDTH / float(image.width)
                image = image.resize(
                    (PREVIEW_MAX_WIDTH, max(1, int(image.height * ratio))),
                    Image.Resampling.LANCZOS,
                )
            buffer = BytesIO()
            image.save(buffer, format='JPEG', quality=PREVIEW_JPEG_QUALITY, optimize=True)
            rendered[index + 1] = buffer.getvalue()
    except Exception as exc:
        logger.warning(
            'marketing_slide_preview_raster_failed',
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )
        return {}
    finally:
        if document is not None:
            document.close()
    return rendered


def render_presentation_previews(
    contents: bytes,
    filename: str,
    slides: list[SlidePayload],
) -> None:
    """Rasterize native PPTX/PDF pages so the CCO queue can show the actual slide."""
    if not contents or not slides:
        return
    missing = [
        slide
        for slide in slides
        if slide.page <= MAX_PREVIEW_SLIDES and slide_preview_image(slide) is None
    ]
    if not missing:
        return
    pdf_bytes = presentation_pdf_bytes(contents, filename)
    if not pdf_bytes:
        return
    jpegs = _pdf_page_jpegs(pdf_bytes, pages=max(slide.page for slide in missing))
    if not jpegs:
        return
    attached = 0
    for slide in missing:
        data = jpegs.get(slide.page)
        if not data:
            continue
        slide.preview_image = SlideImage(
            media_type='image/jpeg',
            data=data,
            name=f'slide-{slide.page}.jpg',
        )
        attached += 1
    if attached:
        logger.info('marketing_slide_previews_rendered', count=attached, filename=filename)


def attach_slide_preview_paths(
    slides: list[SlidePayload],
    *,
    org_id: str,
    content_id: str,
) -> None:
    """Store a full-slide screenshot so the CCO queue can render marked thumbnails."""
    if not org_id or not content_id:
        return
    from app.services.storage_service import storage_path, upload_file

    bucket = 'marketing-contents'
    for slide in slides:
        if slide.image_storage_uri:
            continue
        image = slide_preview_image(slide)
        if image is None or not image.data:
            continue
        suffix = 'jpg' if 'jpeg' in (image.media_type or '') else 'png'
        try:
            stored = upload_file(
                bucket,
                storage_path(org_id, 'marketing', content_id, f'slide-{slide.page}.{suffix}'),
                image.data,
                image.media_type or 'image/jpeg',
            )
            slide.image_storage_uri = stored.uri
        except Exception as exc:
            logger.warning(
                'marketing_slide_preview_store_failed',
                page=slide.page,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )


async def _caption_slide_image(
    image: SlideImage,
    *,
    client: LLMProvider,
    page: int,
) -> str:
    if not image.data:
        return ''
    media_type = sniff_image_media_type(image.data, name=image.name, declared=image.media_type)
    if media_type not in ANTHROPIC_IMAGE_TYPES:
        logger.warning(
            'marketing_diagram_caption_skipped',
            page=page,
            reason='unsupported_media_type',
            media_type=media_type or image.media_type,
        )
        return ''
    image.media_type = media_type
    encoded = base64.standard_b64encode(image.data).decode('ascii')
    prompt = SLIDE_VISION_PROMPT
    last_error = ''
    for attempt in range(2):
        try:
            response = await client.generate(
                [ChatMessage('user', [ImagePart(media_type, encoded), TextPart(prompt)])],
                max_tokens=4_000,
                throttle='sonnet',
                trace_name='marketing.slide_caption',
            )
            caption = response.text.strip()
            if caption:
                return caption
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'[:300]
            logger.warning(
                'marketing_diagram_caption_failed',
                page=page,
                attempt=attempt + 1,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
    logger.warning('marketing_diagram_caption_empty', page=page, error=last_error)
    return ''
