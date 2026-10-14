"""Mode A / Mode B orchestration. Short DB sessions; LLM work is offline."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy.orm import Session

from app.ai import optional_llm_provider
from app.core.config import get_settings
from app.db.session import reset_tenant_org_id, session_scope, set_tenant_org_id
from app.models.core import MarketingContent, MarketingReview
from app.services.marketing_output_adapter import (
    build_change_list_artifact,
    build_tracked_docx_bytes,
    output_kind_for_format,
)
from app.services.marketing_review_engine import (
    cadence_sibling_finding,
    classify_marketing_material,
    drop_empty_review_placeholders,
    finalize_marketing_findings,
    heuristic_review,
    llm_review,
    merge_review_results,
    question_is_optional,
    synthesize_layout_notes,
)
from app.services.marketing_rule_store import load_marketing_rule_bundle
from app.services.marketing_types import (
    CONTENT_STATUS_AWAITING_ANSWERS,
    CONTENT_STATUS_FAILED,
    CONTENT_STATUS_IN_REVIEW,
    CONTENT_STATUS_REVIEWED,
    REVIEW_AWAITING_ANSWERS,
    REVIEW_FAILED,
    REVIEW_READY,
    REVIEW_RUNNING,
)
from app.services.storage_service import storage_path, upload_file

logger = structlog.get_logger(__name__)
VECTOR_GAP_TIMEOUT_SECONDS = 40
LLM_REVIEW_TIMEOUT_SECONDS = 75
SLIDE_CAPTION_TIMEOUT_SECONDS = 180
PRESENTATION_SLIDE_CAPTION_TIMEOUT_SECONDS = 480


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _latest_review(db: Session, *, org_id: str, content_id: str) -> MarketingReview | None:
    return (
        db.query(MarketingReview)
        .filter(MarketingReview.org_id == org_id, MarketingReview.content_id == content_id)
        .order_by(MarketingReview.created_at.desc())
        .first()
    )


async def run_marketing_review(
    *,
    org_id: str,
    content_id: str,
    mode: str = 'A',
    trigger_type: str | None = None,
    trigger_id: str | None = None,
    publication_id: str | None = None,
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    token = set_tenant_org_id(org_id)
    try:
        return await _run_marketing_review_inner(
            org_id=org_id,
            content_id=content_id,
            mode=mode,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            publication_id=publication_id,
            answers=answers or {},
        )
    finally:
        reset_tenant_org_id(token)


async def _run_marketing_review_inner(
    *,
    org_id: str,
    content_id: str,
    mode: str,
    trigger_type: str | None,
    trigger_id: str | None,
    publication_id: str | None,
    answers: dict[str, str],
) -> dict[str, Any]:
    with session_scope(commit=True, org_id=org_id) as db:
        content = (
            db.query(MarketingContent)
            .filter(MarketingContent.id == content_id, MarketingContent.org_id == org_id)
            .one_or_none()
        )
        if content is None:
            return {'status': 'failed', 'message': 'marketing_content_not_found'}
        review = _latest_review(db, org_id=org_id, content_id=content_id)
        if review is None:
            review = MarketingReview(
                id=str(uuid4()),
                org_id=org_id,
                content_id=content_id,
                status=REVIEW_RUNNING,
                findings=[],
                created_at=_now(),
            )
            db.add(review)
        review.mode = mode
        review.trigger_type = trigger_type
        review.trigger_id = trigger_id
        review.publication_id = publication_id
        review.status = REVIEW_RUNNING
        review.updated_at = _now()
        if answers:
            merged = dict(review.answers or {})
            merged.update({key: value for key, value in answers.items() if value is not None})
            review.answers = merged
        content.status = CONTENT_STATUS_IN_REVIEW
        content_snapshot = _content_snapshot(content)
        sibling_names: list[str] = []
        if content.cadence_key:
            sibling_names = [
                row.name
                for row in (
                    db.query(MarketingContent)
                    .filter(
                        MarketingContent.org_id == org_id,
                        MarketingContent.cadence_key == content.cadence_key,
                        MarketingContent.id != content.id,
                    )
                    .limit(8)
                    .all()
                )
            ]
        prior_answers = dict(review.answers or {})
        review_id = review.id

    settings = get_settings()
    from app.services.marketing_ingest_service import is_decoded_image_dump, readable_marketing_body

    if is_decoded_image_dump(str(content_snapshot.get('body_text') or '')):
        content_snapshot['body_text'] = ''
    format_name = str(content_snapshot.get('format') or '').strip().lower()
    slides: list[Any] = []
    transcribed_notes: list[dict[str, Any]] = []
    try:
        slides, transcribed_notes, transcribed = await _transcribe_stored_marketing_file(
            content_snapshot=content_snapshot,
            org_id=org_id,
            content_id=content_id,
            settings=settings,
        )
        if transcribed and not is_decoded_image_dump(transcribed):
            content_snapshot['body_text'] = transcribed
        if transcribed_notes:
            content_snapshot['layout_notes'] = transcribed_notes
        readable = readable_marketing_body(
            str(content_snapshot.get('body_text') or ''),
            transcribed_notes or list(content_snapshot.get('layout_notes') or []),
        )
        if readable and not is_decoded_image_dump(readable):
            content_snapshot['body_text'] = readable
        elif is_decoded_image_dump(str(content_snapshot.get('body_text') or '')):
            content_snapshot['body_text'] = ''
        if readable or transcribed_notes:
            _persist_review_copy(
                org_id=org_id,
                content_id=content_id,
                review_id=review_id,
                body_text=readable,
                layout_notes=transcribed_notes or list(content_snapshot.get('layout_notes') or []),
            )
    except Exception as exc:
        logger.warning('marketing_screenshot_transcribe_failed', error_type=type(exc).__name__)
    if not answers:
        from app.services.marketing_regulation_sync import refresh_marketing_rules_for_review

        await refresh_marketing_rules_for_review(org_id, settings)
    layout_notes = synthesize_layout_notes(
        body_text=content_snapshot['body_text'],
        layout_notes=content_snapshot['layout_notes'],
        format_name=content_snapshot['format'],
    )
    classification = classify_marketing_material(
        format_name=content_snapshot['format'],
        body_text=content_snapshot['body_text'],
        audience=content_snapshot['audience'],
        registrant_type=content_snapshot['registrant_type'],
        layout_notes=layout_notes,
    )
    with session_scope(commit=False, org_id=org_id) as db:
        rule_bundle = load_marketing_rule_bundle(db, org_id=org_id)

    result = heuristic_review(
        classification=classification,
        body_text=content_snapshot['body_text'],
        layout_notes=layout_notes,
        answers=prior_answers,
    )
    vector_gap_payload: dict[str, Any] = {}
    embed_text = ''
    slide_embed_texts: list[str] = []
    run_vector_gap = False
    try:
        from app.services.marketing_slide_service import (
            findings_from_slide_vision,
            slide_embed_corpus,
            slides_from_layout_notes,
        )
        from app.services.marketing_vector_gap import (
            clear_embed_cache,
            marketing_gap_embed_text,
        )

        clear_embed_cache()

        if format_name == 'presentation' and not slides:
            slides = slides_from_layout_notes(
                layout_notes,
                body_text=str(content_snapshot.get('body_text') or ''),
            )
        if slides:
            vision_findings = findings_from_slide_vision(slides)
            if vision_findings:
                result['findings'] = drop_empty_review_placeholders(
                    list(result.get('findings') or []) + vision_findings
                )
            if transcribed_notes:
                layout_notes = synthesize_layout_notes(
                    body_text=content_snapshot['body_text'],
                    layout_notes=transcribed_notes,
                    format_name=format_name,
                )
                result['layout_notes'] = layout_notes
        slide_embed_texts = [blob for _, blob in slide_embed_corpus(slides)] if slides else []
        embed_text = marketing_gap_embed_text(
            body_text=str(content_snapshot.get('body_text') or ''),
            layout_notes=list(layout_notes or []),
            slide_embed_texts=slide_embed_texts,
        )
        run_vector_gap = bool(embed_text)
    except Exception:
        run_vector_gap = bool(content_snapshot.get('body_text'))
        embed_text = str(content_snapshot.get('body_text') or '')
    if run_vector_gap:
        try:
            from app.services.marketing_regulation_sync import resolve_marketing_rule_documents
            from app.services.marketing_vector_gap import (
                analyze_marketing_rule_gap,
                analyze_presentation_slide_gaps,
            )

            rule_documents = await resolve_marketing_rule_documents(org_id, settings)
            if format_name == 'presentation' and slide_embed_texts:
                from app.services.marketing_slide_service import slide_embed_corpus as _corpus

                slide_pairs = (
                    _corpus(slides)
                    if slides
                    else [(index + 1, blob) for index, blob in enumerate(slide_embed_texts)]
                )
                vector_gap = await asyncio.wait_for(
                    analyze_presentation_slide_gaps(
                        slides=slide_pairs,
                        global_text=embed_text,
                        settings=settings,
                        rule_documents=rule_documents,
                    ),
                    timeout=VECTOR_GAP_TIMEOUT_SECONDS,
                )
            else:
                vector_gap = await asyncio.wait_for(
                    analyze_marketing_rule_gap(
                        content_text=embed_text,
                        format_name=format_name,
                        settings=settings,
                        rule_documents=rule_documents,
                    ),
                    timeout=VECTOR_GAP_TIMEOUT_SECONDS,
                )
            vector_gap_payload = {
                **vector_gap.as_metadata_dict(),
                'related_excerpts': vector_gap.related_excerpts,
                'excerpt_text': vector_gap.excerpt_text(),
            }
        except TimeoutError:
            logger.warning('marketing_vector_gap_timeout', format_name=format_name)
            vector_gap_payload = {'error': 'TimeoutError'}
        except Exception as exc:
            logger.warning('marketing_vector_gap_failed', error_type=type(exc).__name__)
            vector_gap_payload = {'error': type(exc).__name__}
    if layout_notes:
        result['layout_notes'] = layout_notes
    client = optional_llm_provider(org_id, 'analysis', platform_model=settings.anthropic_sonnet_model)
    try:
        llm_result = await asyncio.wait_for(
            llm_review(
                client=client,
                classification=classification,
                body_text=content_snapshot['body_text'],
                layout_notes=layout_notes,
                rule_bundle=rule_bundle,
                answers=prior_answers,
                vector_gap=vector_gap_payload,
            ),
            timeout=LLM_REVIEW_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning('marketing_llm_review_timeout', content_id=content_id)
        llm_result = None
    result = merge_review_results(
        result,
        llm_result,
        body_text=content_snapshot['body_text'],
        classification=classification,
    )
    result['findings'] = drop_empty_review_placeholders(list(result.get('findings') or []))
    if slides:
        from app.services.marketing_slide_service import bind_findings_to_slides

        bind_findings_to_slides(result['findings'], slides)
        if format_name in {'email', 'blog', 'social', 'image'}:
            for item in result.get('findings') or []:
                if isinstance(item, dict) and not item.get('slide'):
                    item['slide'] = 1
    sibling = cadence_sibling_finding(
        cadence_key=content_snapshot.get('cadence_key'),
        sibling_names=sibling_names,
    )
    if sibling:
        result['findings'] = list(result.get('findings') or []) + [sibling]
    result['findings'], result['change_list'] = finalize_marketing_findings(
        result['findings'],
        result.get('classification') or classification,
    )
    result['classification'] = result.get('classification') or classification
    try:
        from app.services.marketing_page_screenshot import (
            PAGE_SCREENSHOT_FORMATS,
            apply_region_markers,
        )

        if format_name in PAGE_SCREENSHOT_FORMATS:
            apply_region_markers(list(result.get('findings') or []), page_count=1)
            result['findings'], result['change_list'] = finalize_marketing_findings(
                result['findings'],
                result.get('classification') or classification,
            )
        elif format_name == 'email' and any(
            isinstance(note, dict) and str(note.get('region') or '').startswith('slide_or_page_')
            for note in (layout_notes or [])
        ):
            for item in result.get('findings') or []:
                if isinstance(item, dict) and not item.get('slide'):
                    item['slide'] = 1
    except Exception as exc:
        logger.warning('marketing_page_marker_failed', error_type=type(exc).__name__)

    questions = [item for item in (result.get('questions') or []) if item.get('prompt')]
    required = [item for item in questions if item.get('id') and not question_is_optional(item)]
    pending = bool(required) and not all(item.get('id') in prior_answers for item in required)
    status = REVIEW_AWAITING_ANSWERS if pending else REVIEW_READY
    content_status = CONTENT_STATUS_AWAITING_ANSWERS if pending else CONTENT_STATUS_REVIEWED
    output_kind = output_kind_for_format(content_snapshot['format'], result.get('findings') or [])
    artifact_path = None
    try:
        artifact_path = _store_artifact(
            org_id=org_id,
            content_id=content_id,
            review_id=review_id,
            title=content_snapshot['title'],
            format_name=content_snapshot['format'],
            body_text=content_snapshot['body_text'],
            findings=result.get('findings') or [],
            change_list=result.get('change_list') or [],
            rule_version=str(rule_bundle.get('version') or ''),
            output_kind=output_kind,
            layout_notes=result.get('layout_notes') or layout_notes,
        )
    except Exception as exc:
        logger.warning('marketing_artifact_store_failed', error_type=type(exc).__name__)

    with session_scope(commit=True, org_id=org_id) as db:
        review = (
            db.query(MarketingReview)
            .filter(MarketingReview.id == review_id, MarketingReview.org_id == org_id)
            .one_or_none()
        )
        content = (
            db.query(MarketingContent)
            .filter(MarketingContent.id == content_id, MarketingContent.org_id == org_id)
            .one_or_none()
        )
        if review is None or content is None:
            return {'status': 'failed', 'message': 'marketing_review_missing_after_run'}
        review.findings = result.get('findings') or []
        review.questions = questions
        review.layout_notes = result.get('layout_notes') or layout_notes
        review.classification = result.get('classification') or classification
        review.rule_versions = {
            'version': rule_bundle.get('version'),
            'publication_ids': rule_bundle.get('publication_ids'),
            'unvalidated': rule_bundle.get('unvalidated'),
            'cosine_similarity': {
                key: value
                for key, value in vector_gap_payload.items()
                if key not in {'related_excerpts', 'excerpt_text'}
            } if vector_gap_payload else None,
        }
        review.change_list = result.get('change_list') or []
        review.output_kind = output_kind
        review.artifact_storage_path = artifact_path
        review.status = status
        review.error_message = None
        review.updated_at = _now()
        content.status = content_status
        content.content_tags = (result.get('classification') or classification).get('tags') or []
        content.classification = result.get('classification') or classification
        content.layout_notes = result.get('layout_notes') or layout_notes
        snapshot_body = readable_marketing_body(
            str(content_snapshot.get('body_text') or ''),
            result.get('layout_notes') or layout_notes,
        )
        if snapshot_body:
            content.body_text = snapshot_body[:50_000]
        elif is_decoded_image_dump(str(content.body_text or '')):
            content.body_text = None
        content.updated_at = _now()
        try:
            from app.services.marketing_gap_bridge import persist_marketing_gap_analysis

            with db.begin_nested():
                persist_marketing_gap_analysis(
                    db,
                    content=content,
                    review=review,
                    findings=list(review.findings or []),
                    cosine=vector_gap_payload,
                    analysis_model=client.model_label if client else settings.anthropic_sonnet_model,
                    for_cco_review=False,
                )
        except Exception as exc:
            logger.warning(
                'marketing_gap_persist_failed',
                error_type=type(exc).__name__,
            )
        db.flush()
        return {
            'status': status,
            'review_id': review.id,
            'content_id': content.id,
            'question_count': len(questions),
            'finding_count': len(review.findings or []),
        }


def mark_review_failed(org_id: str, content_id: str, message: str) -> None:
    with session_scope(commit=True, org_id=org_id) as db:
        content = (
            db.query(MarketingContent)
            .filter(MarketingContent.id == content_id, MarketingContent.org_id == org_id)
            .one_or_none()
        )
        review = _latest_review(db, org_id=org_id, content_id=content_id) if content else None
        if content is not None:
            content.status = CONTENT_STATUS_FAILED
            content.updated_at = _now()
        if review is not None:
            review.status = REVIEW_FAILED
            review.error_message = message[:1_000]
            review.updated_at = _now()


def _persist_review_copy(
    *,
    org_id: str,
    content_id: str,
    review_id: str,
    body_text: str,
    layout_notes: list[dict[str, Any]],
) -> None:
    """Store transcribed screenshot copy before the rest of the review so the UI can show it."""
    from app.services.marketing_ingest_service import is_decoded_image_dump, readable_marketing_body

    cleaned = readable_marketing_body(body_text, layout_notes)
    if not cleaned and not layout_notes:
        return
    with session_scope(commit=True, org_id=org_id) as db:
        content = (
            db.query(MarketingContent)
            .filter(MarketingContent.id == content_id, MarketingContent.org_id == org_id)
            .one_or_none()
        )
        review = (
            db.query(MarketingReview)
            .filter(MarketingReview.id == review_id, MarketingReview.org_id == org_id)
            .one_or_none()
        )
        if content is None:
            return
        if cleaned:
            content.body_text = cleaned[:50_000]
        elif is_decoded_image_dump(str(content.body_text or '')):
            content.body_text = None
        if layout_notes:
            content.layout_notes = layout_notes
            if review is not None:
                review.layout_notes = layout_notes
        content.updated_at = _now()
        if review is not None:
            review.updated_at = _now()


async def _transcribe_stored_marketing_file(
    *,
    content_snapshot: dict[str, Any],
    org_id: str,
    content_id: str,
    settings: Any,
) -> tuple[list[Any], list[dict[str, Any]], str]:
    """OCR screenshot/deck images before rule refresh so scraped copy is saved first."""
    from pathlib import Path

    from app.services.marketing_slide_service import (
        MAX_DIAGRAM_IMAGES,
        attach_slide_preview_paths,
        caption_slide_diagrams,
        file_slides_for_marketing_review,
        is_image_file,
        joined_slide_text,
        layout_notes_from_slides,
        presentation_source_filename,
        render_presentation_previews,
    )
    from app.services.storage_service import download_file

    storage = str(content_snapshot.get('storage_path') or '').strip()
    notes = [
        item
        for item in (content_snapshot.get('layout_notes') or [])
        if isinstance(item, dict)
    ]
    file_slides: list[Any] = []
    file_bytes = b''
    source_name = ''
    image_upload = False
    if storage:
        file_bytes = download_file(storage)
        stored_name = Path(storage.split('?')[0]).name
        file_type = str(content_snapshot.get('file_content_type') or '')
        source_name = stored_name or presentation_source_filename(
            storage_path=storage,
            content_type=file_type,
        )
        image_upload = is_image_file(
            filename=source_name,
            content_type=file_type,
            contents=file_bytes,
        )
        file_slides = file_slides_for_marketing_review(
            filename=source_name,
            content_type=file_type,
            contents=file_bytes,
        )
        if file_slides:
            render_presentation_previews(file_bytes, source_name, file_slides)
    if not file_slides:
        file_slides = _slides_from_layout_screenshot_notes(notes, download_file=download_file)
    if not file_slides:
        return [], [], ''

    client = optional_llm_provider(org_id, 'analysis', platform_model=settings.anthropic_sonnet_model)
    multi_page = len(file_slides) > 1 or not image_upload
    caption_timeout = (
        PRESENTATION_SLIDE_CAPTION_TIMEOUT_SECONDS if multi_page else SLIDE_CAPTION_TIMEOUT_SECONDS
    )
    try:
        file_slides = await asyncio.wait_for(
            caption_slide_diagrams(
                file_slides,
                client=client,
                max_images=1 if image_upload and len(file_slides) == 1 else MAX_DIAGRAM_IMAGES,
            ),
            timeout=caption_timeout,
        )
    except TimeoutError:
        logger.warning(
            'marketing_slide_diagram_caption_timeout',
            format_name=str(content_snapshot.get('format') or ''),
        )
    except Exception as exc:
        logger.warning('marketing_slide_diagram_caption_failed', error_type=type(exc).__name__)

    attach_slide_preview_paths(file_slides, org_id=org_id, content_id=content_id)
    transcribed = joined_slide_text(file_slides)
    transcribed_notes = layout_notes_from_slides(file_slides)
    if not transcribed:
        from app.services.marketing_ingest_service import readable_marketing_body

        transcribed = readable_marketing_body('', transcribed_notes)
    logger.info(
        'marketing_slide_images_transcribed',
        slide_count=len(file_slides),
        image_count=sum(len(item.images) for item in file_slides),
        caption_count=sum(len(item.diagram_captions) for item in file_slides),
        transcribed_chars=len(transcribed),
    )
    return file_slides, transcribed_notes, transcribed


def _slides_from_layout_screenshot_notes(
    notes: list[dict[str, Any]],
    *,
    download_file: Any,
) -> list[Any]:
    """Rebuild vision slides from stored screenshot/page images on layout notes."""
    from pathlib import Path

    from app.services.marketing_slide_service import (
        SlideImage,
        SlidePayload,
        is_image_file,
        sniff_image_media_type,
    )

    grouped: dict[int, SlidePayload] = {}
    for note in notes:
        path = str(note.get('slide_image_path') or '').strip()
        region = str(note.get('region') or '')
        label = str(note.get('label') or '').strip().lower()
        font = str(note.get('font_note') or '').strip().lower()
        is_shot = (
            label in {'screenshot', 'page'}
            or font == 'screenshot'
            or region == 'image_capture'
            or (path and region.startswith('slide_or_page_'))
        )
        if not is_shot or not path:
            continue
        try:
            data = download_file(path)
        except Exception:
            continue
        if not data or not is_image_file(filename=path, contents=data):
            continue
        page = int(note.get('page') or 0) or 1
        if region.startswith('slide_or_page_'):
            try:
                page = int(region.rsplit('_', 1)[-1])
            except ValueError:
                pass
        media = sniff_image_media_type(data, name=Path(path).name) or 'image/jpeg'
        image = SlideImage(media_type=media, data=data, name=Path(path).name)
        excerpt = str(note.get('excerpt') or '').strip()
        if excerpt.lower() in {'screenshot', ''} or excerpt.lower().startswith('http'):
            excerpt = ''
        slide = grouped.get(page) or SlidePayload(
            page=page,
            text=excerpt,
            label='screenshot',
        )
        if excerpt and len(excerpt) > len(str(slide.text or '')):
            slide.text = excerpt
        slide.images = [image]
        slide.preview_image = image
        slide.image_storage_uri = path
        grouped[page] = slide
    slides = [grouped[key] for key in sorted(grouped)]
    if not slides:
        return []
    page_count = max(item.page for item in slides)
    for item in slides:
        item.page_count = max(int(item.page_count or 0), page_count)
    return slides


def _content_snapshot(content: MarketingContent) -> dict[str, Any]:
    return {
        'id': content.id,
        'title': content.title or content.name,
        'format': content.format or 'text',
        'audience': content.audience or 'unknown',
        'registrant_type': content.registrant_type or 'unknown',
        'body_text': content.body_text or '',
        'layout_notes': list(content.layout_notes or []),
        'cadence_key': content.cadence_key,
        'storage_path': getattr(content, 'storage_path', None),
        'file_content_type': getattr(content, 'file_content_type', None),
        'source_url': getattr(content, 'source_url', None) or '',
    }


def _store_artifact(
    *,
    org_id: str,
    content_id: str,
    review_id: str,
    title: str,
    format_name: str,
    body_text: str,
    findings: list[dict[str, Any]],
    change_list: list[dict[str, Any]],
    rule_version: str,
    output_kind: str,
    layout_notes: list[dict[str, Any]] | None = None,
    rule_labels: list[str] | None = None,
) -> str | None:
    settings = get_settings()
    if 'marketing-contents' not in settings.supabase_storage_bucket_list:
        return None
    if output_kind == 'tracked_docx' and format_name == 'presentation':
        from app.services.marketing_redline_service import build_marketing_redline_bytes

        payload = build_marketing_redline_bytes(
            title=title,
            format_label='Presentation',
            body_text=body_text,
            rule_labels=rule_labels or ['SEC: Rule 206(4)-1'],
            findings=findings,
            gap_summary='Marketing review — presentation. DRAFT — CCO REVIEW REQUIRED.',
            layout_notes=layout_notes,
        )
        filename = f'{review_id}.docx'
        content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    elif output_kind == 'tracked_docx':
        payload = build_tracked_docx_bytes(
            title=title,
            body_text=body_text,
            findings=findings,
            change_list=change_list,
            rule_version=rule_version,
        )
        filename = f'{review_id}.docx'
        content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    else:
        import json

        payload = json.dumps(
            build_change_list_artifact(
                title=title,
                format_name=format_name,
                findings=findings,
                change_list=change_list,
                rule_version=rule_version,
            ),
            indent=2,
        ).encode('utf-8')
        filename = f'{review_id}.json'
        content_type = 'application/json'
    path = storage_path(org_id, 'marketing-reviews', content_id, filename)
    stored = upload_file('marketing-contents', path, payload, content_type)
    return stored.uri
