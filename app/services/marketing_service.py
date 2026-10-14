"""Marketing collateral upload + Marketing Review Agent workflow."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import structlog
from fastapi import HTTPException, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, load_only

from app.core.config import get_settings
from app.core.security import CurrentUser
from app.db.ordering import desc
from app.models.core import GapAnalysis, MarketingContent, MarketingReview, Policy, PolicyRevision
from app.schemas.marketing import (
    MarketingArchiveTagResponse,
    MarketingContentDetailResponse,
    MarketingContentListResponse,
    MarketingContentResponse,
    MarketingRedlinePrepareResponse,
    MarketingReviewAnswers,
    MarketingReviewDecision,
    MarketingReviewResponse,
    MarketingUploadUrlResponse,
)
from app.services.marketing_ingest_service import (
    assert_safe_public_url,
    canonical_marketing_source_url,
    checksum_sha256,
    extract_marketing_text,
    fetch_public_url_text,
    infer_format_from_inputs,
    layout_notes_from_bytes,
    readable_marketing_body,
    validate_upload_meta,
)
from app.services.marketing_review_engine import classify_marketing_material, synthesize_layout_notes
from app.services.marketing_schema import ensure_marketing_review_schema
from app.services.marketing_slide_service import is_image_file, sniff_image_media_type
from app.services.marketing_types import (
    AUDIENCES,
    CONTENT_STATUS_IN_REVIEW,
    CONTENT_STATUS_QUEUED,
    OPTIONS,
    PERFORMANCE_DISCLAIMER,
    REGISTRANT_TYPES,
    REPORT_HEADER,
    REVIEW_QUEUED,
    REVIEW_RUNNING,
    REVIEW_SIGNED_OFF,
)
from app.services.marketing_vector_gap import primary_marketing_cosine_similarity
from app.services.policy_service import safe_filename
from app.services.storage_service import (
    StoredFile,
    create_signed_upload_url,
    download_file,
    parse_storage_uri,
    remove_file,
    safe_storage_segment,
    storage_path,
    upload_file,
)
from app.services.workflow_dispatch_service import (
    queue_background_task,
    should_run_background_workflow,
)

logger = structlog.get_logger(__name__)
MARKETING_CONTENT_BUCKET = 'marketing-contents'
# Screenshot vision routinely exceeds 15s. Re-queueing that often stacked reviews
# that rebuilt embeddings and exhausted the DB pool.
STALE_MARKETING_REVIEW_SECONDS = 300
_inflight_review_lock = threading.Lock()
_inflight_reviews: set[str] = set()
_review_slots = threading.Semaphore(1)


def assert_org_owned_marketing_path(org_id: str, path: str) -> str:
    normalized = path.strip().lstrip('/')
    prefix = f'{safe_storage_segment(org_id)}/marketing/'
    if not normalized.startswith(prefix) or any(part == '..' for part in normalized.split('/')):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail='Upload path is not in this organization',
        )
    return normalized


def create_marketing_upload_url(
    user: CurrentUser,
    *,
    filename: str,
    content_type: str | None,
    size: int,
) -> MarketingUploadUrlResponse:
    settings = get_settings()
    if MARKETING_CONTENT_BUCKET not in settings.supabase_storage_bucket_list:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Marketing storage is not configured',
        )
    safe_name = safe_filename(filename)
    validate_upload_meta(
        filename=safe_name,
        content_type=content_type,
        size=size,
        max_mb=settings.max_policy_upload_mb,
    )
    content_id = str(uuid4())
    path = storage_path(user.org_id, 'marketing', content_id, safe_name)
    signed = create_signed_upload_url(MARKETING_CONTENT_BUCKET, path)
    return MarketingUploadUrlResponse(
        content_id=content_id,
        path=path,
        signed_url=signed['signed_url'],
        token=signed['token'],
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _latest_review(db: Session, org_id: str, content_id: str) -> MarketingReview | None:
    return (
        db.query(MarketingReview)
        .filter(MarketingReview.org_id == org_id, MarketingReview.content_id == content_id)
        .order_by(desc(MarketingReview.created_at))
        .first()
    )


def _normalize_marketing_title(value: str | None) -> str:
    return ' '.join(str(value or '').strip().lower().split())


def _marketing_content_match_keys(
    *,
    source_url: str | None,
    title: str | None,
    format_name: str | None,
) -> set[str]:
    keys: set[str] = set()
    canonical = canonical_marketing_source_url(source_url or '')
    if canonical:
        keys.add(f'url:{canonical}')
    fmt = str(format_name or '').strip().lower()
    title_key = _normalize_marketing_title(title)
    if fmt in {'webpage', 'blog'} and title_key:
        keys.add(f'title:{fmt}:{title_key}')
    return keys


def _match_keys_for_content(row: MarketingContent) -> set[str]:
    return _marketing_content_match_keys(
        source_url=getattr(row, 'source_url', None),
        title=getattr(row, 'title', None) or getattr(row, 'name', None),
        format_name=getattr(row, 'format', None),
    )


def _content_recency(row: MarketingContent) -> tuple:
    created = getattr(row, 'created_at', None) or datetime.min
    updated = getattr(row, 'updated_at', None) or created
    return (updated, created, str(getattr(row, 'id', '')))


def _cluster_marketing_contents(rows: list[MarketingContent]) -> list[list[MarketingContent]]:
    parent = {id(row): id(row) for row in rows}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: MarketingContent, right: MarketingContent) -> None:
        root_left, root_right = find(id(left)), find(id(right))
        if root_left != root_right:
            parent[root_left] = root_right

    keyed: dict[str, MarketingContent] = {}
    for row in rows:
        keys = _match_keys_for_content(row)
        if not keys:
            keys = {f'id:{getattr(row, "id", id(row))}'}
        for key in keys:
            prior = keyed.get(key)
            if prior is not None:
                union(row, prior)
            keyed[key] = row
    clusters: dict[int, list[MarketingContent]] = {}
    for row in rows:
        clusters.setdefault(find(id(row)), []).append(row)
    return list(clusters.values())


def _preferred_marketing_content(rows: list[MarketingContent]) -> MarketingContent:
    return max(rows, key=_content_recency)


def _matching_marketing_contents(
    db: Session,
    *,
    org_id: str,
    source_url: str,
    title: str,
    format_name: str,
) -> list[MarketingContent]:
    submit_keys = _marketing_content_match_keys(
        source_url=source_url,
        title=title,
        format_name=format_name,
    )
    if not submit_keys:
        return []
    rows = (
        db.query(MarketingContent)
        .filter(MarketingContent.org_id == org_id)
        .order_by(desc(MarketingContent.updated_at), desc(MarketingContent.created_at))
        .all()
    )
    matched = [row for row in rows if submit_keys & _match_keys_for_content(row)]
    matched.sort(key=_content_recency, reverse=True)
    return matched


def _remove_marketing_content_row(
    db: Session,
    *,
    org_id: str,
    record: MarketingContent,
) -> list[str | None]:
    reviews = (
        db.query(MarketingReview)
        .filter(MarketingReview.org_id == org_id, MarketingReview.content_id == record.id)
        .all()
    )
    storage_uris = [getattr(record, 'storage_path', None)]
    storage_uris.extend(getattr(review, 'artifact_storage_path', None) for review in reviews)
    from app.services.marketing_gap_bridge import delete_marketing_gap_rows

    delete_marketing_gap_rows(db, org_id=org_id, content_id=record.id)
    for review in reviews:
        db.delete(review)
    db.delete(record)
    return storage_uris


def _reset_marketing_review_for_rerun(review: MarketingReview) -> None:
    """Clear prior findings so a re-analysis overwrites the same review row."""
    review.status = REVIEW_QUEUED
    review.findings = []
    review.questions = []
    review.answers = {}
    review.layout_notes = []
    review.classification = {}
    review.rule_versions = {}
    review.change_list = []
    review.selected_options = []
    review.output_kind = None
    review.artifact_storage_path = None
    review.error_message = None
    review.decided_by = None
    review.decided_at = None
    review.updated_at = _now()


async def upload_marketing_content(
    file: UploadFile | None,
    user: CurrentUser,
    db: Session | None,
    *,
    title: str | None = None,
    format_name: str | None = None,
    source_url: str | None = None,
    audience: str | None = None,
    registrant_type: str | None = None,
    channel: str | None = None,
    body_text: str | None = None,
    cadence_key: str | None = None,
    cadence_step: int | None = None,
    published_locations: list[str] | None = None,
    background_tasks: object | None = None,
    stored_path: str | None = None,
    preferred_content_id: str | None = None,
    persist: bool = False,
) -> MarketingContentResponse:
    settings = get_settings()
    contents = b''
    filename = safe_filename((file.filename if file is not None else None) or 'marketing-content.txt')
    content_type = file.content_type if file is not None else 'text/plain'
    storage_uri: str | None = None
    if stored_path:
        normalized = assert_org_owned_marketing_path(user.org_id, stored_path)
        filename = safe_filename(Path(normalized).name)
        storage_uri = StoredFile(MARKETING_CONTENT_BUCKET, normalized).uri
        contents = download_file(storage_uri)
        if not content_type or content_type in {'text/plain', 'application/octet-stream'}:
            guessed = {
                '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                '.pdf': 'application/pdf',
                '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                '.png': 'image/png',
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.webp': 'image/webp',
                '.gif': 'image/gif',
            }.get(Path(filename).suffix.lower())
            content_type = guessed or content_type or 'application/octet-stream'
        sniffed = sniff_image_media_type(contents, name=filename, declared=content_type or '')
        if sniffed:
            content_type = sniffed
        validate_upload_meta(
            filename=filename,
            content_type=content_type,
            size=len(contents),
            max_mb=settings.max_policy_upload_mb,
        )
    elif file is not None:
        contents = await file.read()
        validate_upload_meta(
            filename=filename,
            content_type=content_type,
            size=len(contents),
            max_mb=settings.max_policy_upload_mb,
        )
    pasted = (body_text or '').strip()
    url = (source_url or '').strip()
    resolved_format = infer_format_from_inputs(format_name, filename, content_type or '', url)
    html_preview: str | None = None
    page_title = ''
    extracted = ''
    notes: list[dict[str, object]] = []
    if resolved_format == 'webpage':
        if not url:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail='A public https homepage URL is required. Webpage reviews do not take pasted copy or a file.',
            )
        assert_safe_public_url(url)
        page = await fetch_public_url_text(url, settings)
        extracted = page.text
        notes = list(page.notes)
        html_preview = page.html_preview or None
        page_title = page.page_title
        from app.services.marketing_ingest_service import homepage_body_is_thin

        if not extracted or homepage_body_is_thin(extracted, page_title):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='The homepage URL was fetched but no readable content was found.',
            )
    elif resolved_format == 'blog':
        if not pasted and not url and not contents:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail='Provide a public https blog URL, paste the article, or attach a document or screenshot.',
            )
        if pasted:
            extracted = pasted
        elif contents:
            extracted, notes = await _extract_marketing_file(
                filename=filename, content_type=content_type, contents=contents
            )
        if url:
            assert_safe_public_url(url)
            page = await fetch_public_url_text(url, settings, capture='blog')
            html_preview = page.html_preview or None
            page_title = page.page_title
            if not extracted:
                extracted = page.text
                notes = list(page.notes)
        if not extracted and not contents:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail='The blog URL was fetched but no readable content was found.',
            )
    else:
        if not contents and not pasted and not url:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail='Provide file, pasted text, or an https source_url',
            )
        extracted, notes = (
            await _extract_marketing_file(
                filename=filename, content_type=content_type, contents=contents
            )
            if contents
            else ('', [])
        )
        if pasted:
            extracted = f'{pasted}\n\n{extracted}'.strip() if extracted else pasted
        if url:
            assert_safe_public_url(url)
            page = await fetch_public_url_text(url, settings)
            extracted = page.text if not extracted else f'{extracted}\n\n{page.text}'
            notes.extend(page.notes)
            html_preview = page.html_preview or None
            page_title = page.page_title
    resolved_audience = (audience or 'unknown').strip().lower()
    if resolved_audience not in AUDIENCES:
        resolved_audience = 'unknown'
    resolved_registrant = (registrant_type or 'unknown').strip().lower()
    if resolved_registrant not in REGISTRANT_TYPES:
        resolved_registrant = 'unknown'
    notes = synthesize_layout_notes(
        body_text=extracted,
        layout_notes=notes,
        format_name=resolved_format,
    )
    classification = classify_marketing_material(
        format_name=resolved_format,
        body_text=extracted,
        audience=resolved_audience,
        registrant_type=resolved_registrant,
        layout_notes=notes,
    )

    display_name = (
        (title or '').strip()
        or page_title
        or (urlparse(url).hostname if url else '')
        or filename
    )
    preferred_id: str | None = None
    if preferred_content_id:
        try:
            preferred_id = str(UUID(preferred_content_id))
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='Invalid content id') from exc

    from app.db.session import session_scope_or_existing

    with session_scope_or_existing(
        db, org_id=user.org_id, commit=False, open_if_missing=persist
    ) as persist_db:
        if persist_db is None:
            return MarketingContentResponse(
                id=preferred_id or str(uuid4()),
                org_id=user.org_id,
                name=display_name,
                storage_path=storage_uri,
                status=CONTENT_STATUS_QUEUED,
                uploaded_by=user.user_id,
                created_at=_now(),
                title=display_name,
                format=resolved_format,
                content_tags=classification['tags'],
            )
        record, review, content_id = _persist_marketing_upload(
            persist_db,
            user=user,
            settings=settings,
            file=file,
            contents=contents,
            filename=filename,
            content_type=content_type,
            storage_uri=storage_uri,
            url=url,
            extracted=extracted,
            notes=notes,
            html_preview=html_preview,
            classification=classification,
            display_name=display_name,
            resolved_format=resolved_format,
            resolved_audience=resolved_audience,
            resolved_registrant=resolved_registrant,
            channel=channel,
            cadence_key=cadence_key,
            cadence_step=cadence_step,
            published_locations=published_locations,
            preferred_id=preferred_id,
        )
        response = _content_response(record, latest_review=review)

    await _dispatch_review(content_id, user.org_id, background_tasks)
    return response


async def _extract_marketing_file(
    *,
    filename: str,
    content_type: str | None,
    contents: bytes,
) -> tuple[str, list[dict[str, object]]]:
    """Parse decks off the event loop so other API requests keep moving."""
    return await asyncio.to_thread(_extract_marketing_file_sync, filename, content_type, contents)


def _extract_marketing_file_sync(
    filename: str,
    content_type: str | None,
    contents: bytes,
) -> tuple[str, list[dict[str, object]]]:
    extracted = extract_marketing_text(filename=filename, content_type=content_type, contents=contents)
    notes = layout_notes_from_bytes(filename=filename, content_type=content_type, contents=contents)
    return extracted, notes


def _persist_marketing_upload(
    db: Session,
    *,
    user: CurrentUser,
    settings: Any,
    file: UploadFile | None,
    contents: bytes,
    filename: str,
    content_type: str | None,
    storage_uri: str | None,
    url: str,
    extracted: str,
    notes: list[dict[str, object]],
    html_preview: str | None,
    classification: dict[str, Any],
    display_name: str,
    resolved_format: str,
    resolved_audience: str,
    resolved_registrant: str,
    channel: str | None,
    cadence_key: str | None,
    cadence_step: int | None,
    published_locations: list[str] | None,
    preferred_id: str | None,
    _healed: bool = False,
) -> tuple[MarketingContent, MarketingReview, str]:
    if not _healed:
        ensure_marketing_review_schema(db)
    existing = None
    duplicates: list[MarketingContent] = []
    matches = _matching_marketing_contents(
        db,
        org_id=user.org_id,
        source_url=url,
        title=display_name,
        format_name=resolved_format,
    )
    if matches:
        existing = matches[0]
        duplicates = matches[1:]
    content_id = existing.id if existing is not None else (preferred_id or str(uuid4()))
    if existing is None and preferred_id:
        existing = (
            db.query(MarketingContent)
            .filter(MarketingContent.id == preferred_id, MarketingContent.org_id == user.org_id)
            .one_or_none()
        )
        if existing is not None:
            content_id = existing.id
    buckets = settings.supabase_storage_bucket_list
    if storage_uri is None and contents and MARKETING_CONTENT_BUCKET in buckets:
        path = storage_path(user.org_id, 'marketing', content_id, filename)
        stored = upload_file(MARKETING_CONTENT_BUCKET, path, contents, file.content_type if file else content_type)
        storage_uri = stored.uri
    elif storage_uri is None and existing is not None and resolved_format != 'webpage':
        storage_uri = existing.storage_path
    notes = _bind_screenshot_storage(notes, storage_uri)

    now = _now()
    if existing is not None:
        record = existing
        record.name = display_name
        record.storage_path = storage_uri
        record.status = CONTENT_STATUS_QUEUED
        record.uploaded_by = user.user_id
        record.title = display_name
        record.format = resolved_format
        record.channel = (channel or '').strip() or None
        record.audience = resolved_audience
        record.registrant_type = resolved_registrant
        record.source_url = url or None
        record.body_text = extracted or None
        record.html_preview = html_preview
        record.content_tags = classification['tags']
        record.layout_notes = notes
        record.classification = classification
        record.published_locations = published_locations or []
        record.cadence_key = (cadence_key or '').strip() or None
        record.cadence_step = cadence_step
        record.file_content_type = content_type
        record.file_size_bytes = len(contents) or None
        record.checksum_sha256 = checksum_sha256(contents) if contents else None
        record.updated_at = now
        review = _latest_review(db, user.org_id, content_id)
        if review is None:
            review = MarketingReview(
                id=str(uuid4()),
                org_id=user.org_id,
                content_id=content_id,
                status=REVIEW_QUEUED,
                findings=[],
                created_at=now,
                mode='A',
                questions=[],
                answers={},
                updated_at=now,
            )
            db.add(review)
        else:
            _reset_marketing_review_for_rerun(review)
        logger.info(
            'marketing_content_overwritten',
            org_id=user.org_id,
            content_id=content_id,
            source_url=(url or '')[:180],
            duplicate_count=len(duplicates),
        )
    else:
        record = MarketingContent(
            id=content_id,
            org_id=user.org_id,
            name=display_name,
            storage_path=storage_uri,
            status=CONTENT_STATUS_QUEUED,
            uploaded_by=user.user_id,
            created_at=now,
            title=display_name,
            format=resolved_format,
            channel=(channel or '').strip() or None,
            audience=resolved_audience,
            registrant_type=resolved_registrant,
            source_url=url or None,
            body_text=extracted or None,
            html_preview=html_preview,
            content_tags=classification['tags'],
            layout_notes=notes,
            classification=classification,
            published_locations=published_locations or [],
            cadence_key=(cadence_key or '').strip() or None,
            cadence_step=cadence_step,
            file_content_type=content_type,
            file_size_bytes=len(contents) or None,
            checksum_sha256=checksum_sha256(contents) if contents else None,
            updated_at=now,
        )
        review = MarketingReview(
            id=str(uuid4()),
            org_id=user.org_id,
            content_id=content_id,
            status=REVIEW_QUEUED,
            findings=[],
            created_at=now,
            mode='A',
            questions=[],
            answers={},
            updated_at=now,
        )
        db.add(record)
        db.add(review)
    stale_uris: list[str | None] = []
    try:
        for duplicate in duplicates:
            stale_uris.extend(
                _remove_marketing_content_row(db, org_id=user.org_id, record=duplicate)
            )
        db.commit()
        db.refresh(record)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'marketing_content_persist_failed',
            org_id=user.org_id,
            content_id=content_id,
            error_type=type(exc).__name__,
        )
        if not _healed:
            ensure_marketing_review_schema(db, force=True)
            return _persist_marketing_upload(
                db,
                user=user,
                settings=settings,
                file=file,
                contents=contents,
                filename=filename,
                content_type=content_type,
                storage_uri=storage_uri,
                url=url,
                extracted=extracted,
                notes=notes,
                html_preview=html_preview,
                classification=classification,
                display_name=display_name,
                resolved_format=resolved_format,
                resolved_audience=resolved_audience,
                resolved_registrant=resolved_registrant,
                channel=channel,
                cadence_key=cadence_key,
                cadence_step=cadence_step,
                published_locations=published_locations,
                preferred_id=preferred_id,
                _healed=True,
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to persist the marketing content record',
        ) from exc
    for uri in stale_uris:
        _best_effort_remove_storage(uri)
    return record, review, content_id


def _with_load_only(query: Any, *columns: Any) -> Any:
    options = getattr(query, 'options', None)
    if not callable(options):
        return query
    try:
        return query.options(load_only(*columns))
    except Exception:
        return query


def _list_marketing_content_rows(db: Session, org_id: str) -> list[MarketingContent]:
    query = (
        db.query(MarketingContent)
        .filter(MarketingContent.org_id == org_id)
        .order_by(desc(MarketingContent.created_at))
        .limit(200)
    )
    column_sets = (
        (
            MarketingContent.id,
            MarketingContent.org_id,
            MarketingContent.name,
            MarketingContent.storage_path,
            MarketingContent.status,
            MarketingContent.uploaded_by,
            MarketingContent.created_at,
            MarketingContent.title,
            MarketingContent.format,
            MarketingContent.source_url,
            MarketingContent.content_tags,
            MarketingContent.updated_at,
        ),
        (
            MarketingContent.id,
            MarketingContent.org_id,
            MarketingContent.name,
            MarketingContent.storage_path,
            MarketingContent.status,
            MarketingContent.uploaded_by,
            MarketingContent.created_at,
        ),
    )
    last_error: Exception | None = None
    for columns in column_sets:
        try:
            return list(_with_load_only(query, *columns).all())
        except SQLAlchemyError as exc:
            last_error = exc
            logger.warning(
                'marketing_content_list_query_failed',
                org_id=org_id,
                error_type=type(exc).__name__,
            )
    if last_error is not None:
        raise last_error
    return []


_CLOSED_CCO_CONTENT_STATUSES = frozenset({'approved', 'rejected'})
_CLOSED_CCO_REVISION_STATUSES = frozenset({'APPROVED', 'REJECTED', 'PARTIALLY_ACCEPTED'})


def _pending_cco_review_content_ids(
    db: Session,
    org_id: str,
    content_ids: list[str],
) -> set[str]:
    """Content that already has a pending CCO Review Queue revision."""
    if not content_ids:
        return set()
    try:
        rows = (
            db.query(GapAnalysis.trigger_id, GapAnalysis.retrieval_metadata)
            .join(PolicyRevision, PolicyRevision.gap_analysis_id == GapAnalysis.id)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.trigger_type == 'marketing_content',
                PolicyRevision.org_id == org_id,
                PolicyRevision.status == 'PENDING',
                PolicyRevision.review_requested_at.is_not(None),
            )
            .all()
        )
    except Exception:
        logger.warning('marketing_content_pending_lookup_failed', org_id=org_id)
        return set()
    wanted = {str(item) for item in content_ids if str(item).strip()}
    pending: set[str] = set()
    for row in rows:
        try:
            trigger_id, metadata = row
        except (TypeError, ValueError):
            continue
        meta = metadata if isinstance(metadata, dict) else {}
        content_id = str(meta.get('marketing_content_id') or trigger_id or '').strip()
        if content_id and content_id in wanted:
            pending.add(content_id)
    return pending


def _cco_closed_marketing_content_ids(db: Session, org_id: str) -> set[str]:
    """Content whose CCO already chose Approve, Reject, or Partially Accepted."""
    try:
        rows = (
            db.query(GapAnalysis.trigger_id, GapAnalysis.retrieval_metadata, PolicyRevision.status)
            .join(PolicyRevision, PolicyRevision.gap_analysis_id == GapAnalysis.id)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.trigger_type == 'marketing_content',
                PolicyRevision.org_id == org_id,
                PolicyRevision.status.in_(_CLOSED_CCO_REVISION_STATUSES),
            )
            .all()
        )
    except Exception:
        logger.warning('marketing_content_closed_lookup_failed', org_id=org_id)
        return set()
    closed: set[str] = set()
    for row in rows:
        try:
            trigger_id, metadata, _status = row
        except (TypeError, ValueError):
            continue
        meta = metadata if isinstance(metadata, dict) else {}
        content_id = str(meta.get('marketing_content_id') or trigger_id or '').strip()
        if content_id:
            closed.add(content_id)
    return closed


def list_marketing_contents(user: CurrentUser, db: Session | None) -> MarketingContentListResponse:
    if db is None:
        return MarketingContentListResponse(items=[], total=0)

    ensure_marketing_review_schema(db)

    try:
        rows = _list_marketing_content_rows(db, user.org_id)
    except Exception:
        logger.exception('marketing_content_list_failed', org_id=user.org_id)
        return MarketingContentListResponse(items=[], total=0)
    try:
        visible = [_preferred_marketing_content(cluster) for cluster in _cluster_marketing_contents(rows)]
        closed_ids = _cco_closed_marketing_content_ids(db, user.org_id)
        visible = [
            row
            for row in visible
            if str(getattr(row, 'status', '') or '').strip().lower() not in _CLOSED_CCO_CONTENT_STATUSES
            and str(getattr(row, 'id', '') or '') not in closed_ids
        ]
        visible.sort(key=lambda row: getattr(row, 'created_at', None) or datetime.min, reverse=True)
    except Exception:
        logger.exception('marketing_content_list_cluster_failed', org_id=user.org_id)
        visible = rows
    try:
        latest_by_content = _latest_reviews_for(db, user.org_id, [row.id for row in visible])
    except Exception:
        logger.exception('marketing_content_list_reviews_failed', org_id=user.org_id)
        latest_by_content = {}
    pending_ids = _pending_cco_review_content_ids(db, user.org_id, [row.id for row in visible])
    items: list[MarketingContentResponse] = []
    for row in visible:
        try:
            items.append(
                _content_response(
                    row,
                    latest_review=latest_by_content.get(row.id),
                    include_layout_notes=False,
                    lite=True,
                    cco_review_in_progress=row.id in pending_ids,
                )
            )
        except Exception:
            logger.exception(
                'marketing_content_list_item_failed',
                org_id=user.org_id,
                content_id=getattr(row, 'id', None),
            )
    return MarketingContentListResponse(items=items, total=len(items))


def _review_age_seconds(review: MarketingReview) -> float:
    stamp = review.updated_at or review.created_at
    if stamp is None:
        return STALE_MARKETING_REVIEW_SECONDS
    current = datetime.now(UTC) if getattr(stamp, 'tzinfo', None) else _now()
    return max(0.0, (current - stamp).total_seconds())


def _resume_stale_marketing_review(
    db: Session,
    *,
    record: MarketingContent,
    review: MarketingReview | None,
    background_tasks: object | None,
) -> None:
    """Re-queue a homepage/review job that never left queued/running."""
    if review is None or background_tasks is None:
        return
    pending_review = review.status in {REVIEW_QUEUED, REVIEW_RUNNING}
    pending_content = record.status in {CONTENT_STATUS_QUEUED, CONTENT_STATUS_IN_REVIEW}
    if not pending_review and not pending_content:
        return
    if review.status not in {REVIEW_QUEUED, REVIEW_RUNNING}:
        return
    if record.id in _inflight_reviews:
        return
    if _review_age_seconds(review) < STALE_MARKETING_REVIEW_SECONDS:
        return
    queued = queue_background_task(
        background_tasks,
        _run_review_job,
        record.id,
        record.org_id,
        {},
    )
    if not queued:
        return
    review.updated_at = _now()
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning(
            'marketing_stale_review_resume_commit_failed',
            content_id=record.id,
            org_id=record.org_id,
        )
        return
    logger.info(
        'marketing_stale_review_requeued',
        content_id=record.id,
        org_id=record.org_id,
        review_id=review.id,
        review_status=review.status,
    )


def get_marketing_content(
    content_id: str,
    user: CurrentUser,
    db: Session | None,
    background_tasks: object | None = None,
) -> MarketingContentDetailResponse:
    if db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail='Database is not configured')
    record = (
        db.query(MarketingContent)
        .filter(MarketingContent.id == content_id, MarketingContent.org_id == user.org_id)
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Marketing content not found')
    review = _latest_review(db, user.org_id, content_id)
    _resume_stale_marketing_review(
        db,
        record=record,
        review=review,
        background_tasks=background_tasks,
    )
    pending_ids = _pending_cco_review_content_ids(db, user.org_id, [content_id])
    base = _content_response(
        record,
        latest_review=review,
        cco_review_in_progress=content_id in pending_ids,
    )
    payload = base.model_dump()
    full_body = readable_marketing_body(
        getattr(record, 'body_text', None),
        _json_list(getattr(review, 'layout_notes', None) if review else None)
        or _json_list(getattr(record, 'layout_notes', None)),
    )
    payload['body_text'] = full_body or None
    revision_id, policy_name = _cco_revision_for_content(db, user.org_id, content_id)
    return MarketingContentDetailResponse(
        **payload,
        html_preview=record.html_preview,
        latest_review=(
            _review_response(
                review,
                record,
                cco_revision_id=revision_id,
                cco_policy_name=policy_name,
            )
            if review
            else None
        ),
    )


def delete_marketing_content(
    content_id: str,
    user: CurrentUser,
    db: Session | None,
) -> None:
    """Remove one org-scoped marketing piece and its reviews from the CCO queue."""
    if db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail='Database is not configured')
    record = (
        db.query(MarketingContent)
        .filter(MarketingContent.id == content_id, MarketingContent.org_id == user.org_id)
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Marketing content not found')
    try:
        storage_uris = _remove_marketing_content_row(db, org_id=user.org_id, record=record)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'marketing_content_delete_failed',
            org_id=user.org_id,
            content_id=content_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to delete the marketing content record',
        ) from exc
    for uri in storage_uris:
        _best_effort_remove_storage(uri)
    logger.info(
        'marketing_content_deleted',
        org_id=user.org_id,
        content_id=content_id,
    )


def _best_effort_remove_storage(uri: str | None) -> None:
    if not uri:
        return
    try:
        remove_file(parse_storage_uri(uri))
    except Exception:
        logger.warning('marketing_storage_cleanup_failed', uri=uri[:180])


def start_marketing_review(
    content_id: str,
    user: CurrentUser,
    db: Session | None,
    background_tasks: object | None = None,
) -> MarketingReviewResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Marketing review is unavailable without a configured database',
        )

    content = (
        db.query(MarketingContent)
        .filter(MarketingContent.id == content_id, MarketingContent.org_id == user.org_id)
        .one_or_none()
    )
    if content is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Marketing content not found')

    review = _latest_review(db, user.org_id, content_id)
    if review is None:
        review = MarketingReview(
            id=str(uuid4()),
            org_id=user.org_id,
            content_id=content_id,
            status=REVIEW_QUEUED,
            findings=[],
            created_at=_now(),
            mode='A',
        )
        db.add(review)
    else:
        _reset_marketing_review_for_rerun(review)
    content.status = CONTENT_STATUS_QUEUED
    content.updated_at = _now()
    try:
        db.commit()
        db.refresh(review)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to start the marketing content review',
        ) from exc
    return _review_response(review)


async def start_marketing_review_and_queue(
    content_id: str,
    user: CurrentUser,
    db: Session | None,
    background_tasks: object | None = None,
) -> MarketingReviewResponse:
    review = start_marketing_review(content_id, user, db, background_tasks)
    await _dispatch_review(content_id, user.org_id, background_tasks)
    return review


async def answer_marketing_review(
    review_id: str,
    payload: MarketingReviewAnswers,
    user: CurrentUser,
    db: Session | None,
    background_tasks: object | None = None,
) -> MarketingReviewResponse:
    if db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail='Database is not configured')
    review = (
        db.query(MarketingReview)
        .filter(MarketingReview.id == review_id, MarketingReview.org_id == user.org_id)
        .one_or_none()
    )
    if review is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail='Marketing review not found')
    cleaned = {
        key.strip()[:80]: value.strip()[:2_000]
        for key, value in (payload.answers or {}).items()
        if str(key).strip() and str(value).strip()
    }
    if not cleaned:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail='Provide at least one answer')
    merged = dict(review.answers or {})
    merged.update(cleaned)
    review.answers = merged
    review.updated_at = _now()
    try:
        db.commit()
        db.refresh(review)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to save answers') from exc
    await _dispatch_review(review.content_id, user.org_id, background_tasks, answers=cleaned)
    return _review_response(review)


def decide_marketing_review(
    review_id: str,
    payload: MarketingReviewDecision,
    user: CurrentUser,
    db: Session | None,
    background_tasks: object | None = None,
) -> MarketingReviewResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Marketing review is unavailable without a configured database',
        )

    review = (
        db.query(MarketingReview)
        .filter(MarketingReview.id == review_id, MarketingReview.org_id == user.org_id)
        .one_or_none()
    )
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Marketing review not found')

    selected = _parse_marketing_selected_options(payload.selected_options)
    if payload.status in {'approved', 'rejected'} and not selected:
        # Legacy stub clients: record sign-off without treating it as an agent approval of the ad.
        selected = [{'finding_id': 'legacy', 'option': 'do_nothing'}]
    if not selected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail='Select an option for each finding before sending to CCO review.',
        )

    review.status = REVIEW_SIGNED_OFF
    review.selected_options = selected
    if payload.findings is not None:
        review.findings = payload.findings
    review.decided_by = user.user_id
    review.decided_at = _now()
    review.updated_at = _now()

    content = (
        db.query(MarketingContent)
        .filter(
            MarketingContent.id == review.content_id,
            MarketingContent.org_id == user.org_id,
        )
        .one_or_none()
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Marketing content not found',
        )
    content.status = 'reviewed'
    content.updated_at = _now()

    notify_payload: dict[str, object] | None = None
    try:
        from app.services.marketing_redline_service import send_marketing_review_to_cco_queue

        revision, notify_payload = send_marketing_review_to_cco_queue(
            db,
            content=content,
            review=review,
            selected_options=selected,
            background_tasks=background_tasks,
        )
        if revision is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Unable to send this marketing review to the CCO Review Queue.',
            )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception(
            'marketing_send_to_cco_review_failed',
            review_id=review.id,
            content_id=content.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to send this marketing review to CCO review',
        ) from exc

    try:
        db.commit()
        db.refresh(review)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to record the marketing review decision',
        ) from exc

    if notify_payload and background_tasks is not None:
        from app.services.redline_generation_service import notify_new_revision_from_payload

        add_task = getattr(background_tasks, 'add_task', None)
        if callable(add_task):
            add_task(
                notify_new_revision_from_payload,
                notify_payload,
                get_settings(),
            )

    revision_id, policy_name = _cco_revision_for_content(db, user.org_id, content.id)
    if revision is not None:
        revision_id = revision.id
    return _review_response(
        review,
        content,
        cco_revision_id=revision_id,
        cco_policy_name=policy_name,
    )


def prepare_marketing_review_redline(
    review_id: str,
    payload: MarketingReviewDecision,
    user: CurrentUser,
    db: Session | None,
) -> MarketingRedlinePrepareResponse:
    """Create or refresh the CCO-queue redline revision without sending it to the queue."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Marketing review is unavailable without a configured database',
        )

    review = (
        db.query(MarketingReview)
        .filter(MarketingReview.id == review_id, MarketingReview.org_id == user.org_id)
        .one_or_none()
    )
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Marketing review not found')

    selected = _parse_marketing_selected_options(payload.selected_options)
    if not selected:
        selected = _parse_marketing_selected_options(list(review.selected_options or []))
    if not selected:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail='Select an option for each finding before downloading the redline.',
        )

    previous = list(review.selected_options or [])
    review.selected_options = selected
    review.updated_at = _now()

    content = (
        db.query(MarketingContent)
        .filter(
            MarketingContent.id == review.content_id,
            MarketingContent.org_id == user.org_id,
        )
        .one_or_none()
    )
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Marketing content not found',
        )

    try:
        from app.services.marketing_redline_service import send_marketing_review_to_cco_queue

        revision, _notify = send_marketing_review_to_cco_queue(
            db,
            content=content,
            review=review,
            selected_options=selected,
            enqueue=False,
            force_rebuild=previous != selected,
        )
        if revision is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail='Unable to prepare a redline for this marketing review.',
            )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.exception(
            'marketing_prepare_redline_failed',
            review_id=review.id,
            content_id=content.id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to prepare the marketing redline',
        ) from exc

    try:
        db.commit()
        db.refresh(review)
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to prepare the marketing redline',
        ) from exc

    policy = (
        db.query(Policy)
        .filter(Policy.id == revision.policy_id, Policy.org_id == user.org_id)
        .one_or_none()
    )
    policy_name = str(
        getattr(policy, 'name', None) or getattr(content, 'title', None) or content.name or 'Marketing'
    )
    return MarketingRedlinePrepareResponse(revision_id=revision.id, policy_name=policy_name)


def tag_untagged_archive(user: CurrentUser, db: Session | None) -> MarketingArchiveTagResponse:
    """Onboarding helper: classify pieces that still have empty tags."""
    if db is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail='Database is not configured')
    rows = (
        db.query(MarketingContent)
        .filter(MarketingContent.org_id == user.org_id)
        .order_by(MarketingContent.created_at.asc())
        .limit(500)
        .all()
    )
    tagged = 0
    skipped = 0
    for row in rows:
        if row.content_tags:
            skipped += 1
            continue
        row.classification = classify_marketing_material(
            format_name=row.format or 'text',
            body_text=row.body_text or '',
            audience=row.audience or 'unknown',
            registrant_type=row.registrant_type or 'unknown',
            layout_notes=list(row.layout_notes or []),
        )
        row.content_tags = row.classification.get('tags') or []
        row.updated_at = _now()
        tagged += 1
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail='Unable to tag archive') from exc
    return MarketingArchiveTagResponse(tagged=tagged, skipped=skipped)


def _latest_reviews_for(
    db: Session,
    org_id: str,
    content_ids: list[str],
) -> dict[str, MarketingReview]:
    if not content_ids:
        return {}
    query = (
        db.query(MarketingReview)
        .filter(MarketingReview.org_id == org_id, MarketingReview.content_id.in_(content_ids))
        .order_by(desc(MarketingReview.created_at))
    )
    rows = list(
        _with_load_only(
            query,
            MarketingReview.id,
            MarketingReview.org_id,
            MarketingReview.content_id,
            MarketingReview.status,
            MarketingReview.created_at,
            MarketingReview.rule_versions,
        ).all()
    )
    latest: dict[str, MarketingReview] = {}
    for row in rows:
        latest.setdefault(row.content_id, row)
    return latest


async def _dispatch_review(
    content_id: str,
    org_id: str,
    background_tasks: object | None,
    answers: dict[str, str] | None = None,
) -> None:

    settings = get_settings()
    delivered = False
    if settings.inngest_async_dispatch_enabled and not answers:
        try:
            import inngest

            from app.core.inngest import create_inngest_client

            client = create_inngest_client(settings)
            await client.send(
                inngest.Event(
                    name='marketing.content.submitted',
                    data={'content_id': content_id, 'org_id': org_id},
                )
            )
            delivered = True
        except Exception as exc:
            logger.warning('marketing_inngest_send_failed', error_type=type(exc).__name__)
    queued = queue_background_task(
        background_tasks,
        _run_review_job,
        content_id,
        org_id,
        answers or {},
    )
    if queued:
        return
    if should_run_background_workflow(settings, delivered) or answers:
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run_review_job, content_id, org_id, answers or {})


def _run_review_job(content_id: str, org_id: str, answers: dict[str, str]) -> None:
    """Sync FastAPI background entrypoint so the API event loop stays free for GETs."""
    with _inflight_review_lock:
        if content_id in _inflight_reviews:
            logger.info('marketing_review_already_running', content_id=content_id, org_id=org_id)
            return
        _inflight_reviews.add(content_id)
    _review_slots.acquire()
    try:
        asyncio.run(_execute_marketing_review(content_id, org_id, answers or {}))
    finally:
        _review_slots.release()
        with _inflight_review_lock:
            _inflight_reviews.discard(content_id)


async def _execute_marketing_review(content_id: str, org_id: str, answers: dict[str, str]) -> None:
    from app.services.marketing_review_workflow import mark_review_failed, run_marketing_review

    try:
        await run_marketing_review(
            org_id=org_id,
            content_id=content_id,
            mode='A',
            answers=answers or None,
        )
    except Exception as exc:
        logger.exception('marketing_review_background_failed', content_id=content_id, org_id=org_id)
        mark_review_failed(org_id, content_id, str(exc))


async def _run_review_background(content_id: str, org_id: str, answers: dict[str, str]) -> None:
    """Test helper: run one review on the current event loop with inflight dedupe."""
    with _inflight_review_lock:
        if content_id in _inflight_reviews:
            logger.info('marketing_review_already_running', content_id=content_id, org_id=org_id)
            return
        _inflight_reviews.add(content_id)
    try:
        await _execute_marketing_review(content_id, org_id, answers)
    finally:
        with _inflight_review_lock:
            _inflight_reviews.discard(content_id)


def _json_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _json_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cco_review_in_progress_flag(
    record: MarketingContent,
    latest_review: MarketingReview | None,
    *,
    pending: bool = False,
) -> bool:
    if pending:
        return True
    review_status = str(getattr(latest_review, 'status', None) or '').strip().lower()
    content_status = str(getattr(record, 'status', None) or '').strip().lower()
    return review_status == REVIEW_SIGNED_OFF or content_status == 'reviewed'


def _content_response(
    record: MarketingContent,
    latest_review: MarketingReview | None = None,
    *,
    include_layout_notes: bool = True,
    lite: bool = False,
    cco_review_in_progress: bool = False,
) -> MarketingContentResponse:
    created_at = getattr(record, 'created_at', None) or _now()
    in_progress = _cco_review_in_progress_flag(
        record,
        latest_review,
        pending=cco_review_in_progress,
    )
    cosine_similarity = primary_marketing_cosine_similarity(
        getattr(latest_review, 'rule_versions', None) if latest_review is not None else None
    )
    if lite:
        return MarketingContentResponse(
            id=record.id,
            org_id=record.org_id,
            name=record.name,
            storage_path=getattr(record, 'storage_path', None),
            status=record.status,
            uploaded_by=getattr(record, 'uploaded_by', None),
            created_at=created_at,
            title=getattr(record, 'title', None) or record.name,
            format=getattr(record, 'format', None) or 'text',
            source_url=getattr(record, 'source_url', None),
            content_tags=[str(item) for item in _json_list(getattr(record, 'content_tags', None))],
            latest_review_id=latest_review.id if latest_review else None,
            latest_review_status=latest_review.status if latest_review else None,
            cco_review_in_progress=in_progress,
            cosine_similarity=cosine_similarity,
        )
    layout_notes: list[dict[str, Any]] = []
    if include_layout_notes:
        layout_notes = _with_signed_slide_images(
            _with_source_screenshot(record, _json_list(getattr(record, 'layout_notes', None)))
        )
    return MarketingContentResponse(
        id=record.id,
        org_id=record.org_id,
        name=record.name,
        storage_path=record.storage_path,
        status=record.status,
        uploaded_by=record.uploaded_by,
        created_at=created_at,
        title=getattr(record, 'title', None) or record.name,
        format=getattr(record, 'format', None) or 'text',
        channel=getattr(record, 'channel', None),
        audience=getattr(record, 'audience', None) or 'unknown',
        registrant_type=getattr(record, 'registrant_type', None) or 'unknown',
        source_url=getattr(record, 'source_url', None),
        body_text=readable_marketing_body(
            getattr(record, 'body_text', None),
            _json_list(getattr(record, 'layout_notes', None)),
        )[:4_000] or None,
        content_tags=[str(item) for item in _json_list(getattr(record, 'content_tags', None))],
        layout_notes=layout_notes,
        classification=_json_dict(getattr(record, 'classification', None)),
        published_locations=[str(item) for item in _json_list(getattr(record, 'published_locations', None))],
        cadence_key=getattr(record, 'cadence_key', None),
        cadence_step=_optional_int(getattr(record, 'cadence_step', None)),
        latest_review_id=latest_review.id if latest_review else None,
        latest_review_status=latest_review.status if latest_review else None,
        cco_review_in_progress=in_progress,
        cosine_similarity=cosine_similarity,
    )


def _bind_screenshot_storage(
    notes: list[dict[str, object]],
    storage_uri: str | None,
) -> list[dict[str, object]]:
    path = (storage_uri or '').strip()
    if not path:
        return notes
    bound: list[dict[str, object]] = []
    for note in notes:
        item = dict(note)
        region = str(item.get('region') or '')
        if region == 'image_capture' or (
            region.startswith('slide_or_page_') and str(item.get('label') or '') == 'screenshot'
        ):
            item['region'] = region if region.startswith('slide_or_page_') else 'slide_or_page_1'
            item['page'] = item.get('page') or 1
            item['page_count'] = item.get('page_count') or 1
            item['slide_image_path'] = path
            item['label'] = 'screenshot'
            item['font_note'] = item.get('font_note') or 'screenshot'
            item['image_count'] = item.get('image_count') or 1
        bound.append(item)
    return bound


def _with_source_screenshot(
    record: MarketingContent,
    notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items = [dict(note) for note in notes if isinstance(note, dict)]
    path = str(getattr(record, 'storage_path', None) or '').strip()
    fmt = str(getattr(record, 'format', None) or '').strip().lower()
    if not path or fmt not in {'blog', 'email', 'social', 'image'}:
        return items
    if any(str(note.get('slide_image_path') or note.get('slide_image_url') or '').strip() for note in items):
        return items
    name = Path(path.split('?')[0]).name
    mime = str(getattr(record, 'file_content_type', None) or '')
    capture = any(
        str(note.get('region') or '') == 'image_capture'
        or str(note.get('region') or '').startswith('slide_or_page_')
        or str(note.get('label') or '') == 'screenshot'
        for note in items
    )
    if not capture and not is_image_file(filename=name, content_type=mime):
        suffix = Path(name).suffix.lower()
        if mime not in {'', 'application/octet-stream', 'binary/octet-stream', 'text/plain'} or suffix in {
            '.doc',
            '.docx',
            '.pdf',
            '.pptx',
            '.html',
            '.txt',
        }:
            return items
    excerpt = readable_marketing_body(getattr(record, 'body_text', None), items)
    injected = {
        'region': 'slide_or_page_1',
        'page': 1,
        'page_count': 1,
        'slide_image_path': path,
        'font_note': 'screenshot',
        'label': 'screenshot',
        'image_count': 1,
        'excerpt': (excerpt or '')[:8_000],
    }
    rest = [note for note in items if str(note.get('region') or '') != 'image_capture']
    if any(str(note.get('region') or '').startswith('slide_or_page_') for note in rest):
        return [
            {
                **note,
                'slide_image_path': note.get('slide_image_path') or path,
                'excerpt': note.get('excerpt') or excerpt,
            }
            if str(note.get('region') or '').startswith('slide_or_page_')
            else note
            for note in rest
        ]
    return [injected] + rest


def _with_signed_slide_images(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signed: list[dict[str, Any]] = []
    for note in notes:
        if not isinstance(note, dict):
            continue
        item = dict(note)
        path = str(item.get('slide_image_path') or '').strip()
        if path:
            try:
                from app.services.storage_service import create_inline_url

                item['slide_image_url'] = create_inline_url(path)
            except Exception as exc:
                logger.warning(
                    'marketing_slide_preview_sign_failed',
                    error_type=type(exc).__name__,
                )
        signed.append(item)
    return signed


def _parse_marketing_selected_options(
    payload_options: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for item in payload_options or []:
        finding_id = str(item.get('finding_id') or '').strip()
        option = str(item.get('option') or '').strip()
        if finding_id and option in OPTIONS and option != 'held':
            selected.append({'finding_id': finding_id, 'option': option})
    return selected


def _cco_revision_for_content(
    db: Session,
    org_id: str,
    content_id: str,
) -> tuple[str | None, str | None]:
    """Latest pending marketing PolicyRevision for this content, if any."""
    wanted = str(content_id or '').strip()
    if not wanted:
        return None, None
    try:
        gap = (
            db.query(GapAnalysis)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.trigger_type == 'marketing_content',
                GapAnalysis.trigger_id == wanted,
            )
            .order_by(GapAnalysis.created_at.desc())
            .first()
        )
        if gap is None:
            return None, None
        revision = (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.gap_analysis_id == gap.id,
                PolicyRevision.org_id == org_id,
                PolicyRevision.status == 'PENDING',
            )
            .order_by(PolicyRevision.created_at.desc())
            .first()
        )
        if revision is None:
            return None, None
        policy = (
            db.query(Policy)
            .filter(Policy.id == revision.policy_id, Policy.org_id == org_id)
            .one_or_none()
        )
        policy_name = str(getattr(policy, 'name', None) or '').strip() or None
        return revision.id, policy_name
    except Exception:
        logger.warning('marketing_cco_revision_lookup_failed', org_id=org_id, content_id=wanted)
        return None, None


def _review_response(
    review: MarketingReview,
    record: MarketingContent | None = None,
    *,
    cco_revision_id: str | None = None,
    cco_policy_name: str | None = None,
) -> MarketingReviewResponse:
    notes = _json_list(getattr(review, 'layout_notes', None))
    if record is not None:
        notes = _with_source_screenshot(record, notes)
    return MarketingReviewResponse(
        id=review.id,
        org_id=review.org_id,
        content_id=review.content_id,
        status=review.status,
        findings=list(review.findings or []),
        decided_by=review.decided_by,
        decided_at=review.decided_at,
        created_at=review.created_at,
        mode=getattr(review, 'mode', None) or 'A',
        trigger_type=getattr(review, 'trigger_type', None),
        trigger_id=getattr(review, 'trigger_id', None),
        publication_id=getattr(review, 'publication_id', None),
        questions=list(getattr(review, 'questions', None) or []),
        answers=dict(getattr(review, 'answers', None) or {}),
        layout_notes=_with_signed_slide_images(notes),
        classification=dict(getattr(review, 'classification', None) or {}),
        rule_versions=dict(getattr(review, 'rule_versions', None) or {}),
        change_list=list(getattr(review, 'change_list', None) or []),
        selected_options=list(getattr(review, 'selected_options', None) or []),
        output_kind=getattr(review, 'output_kind', None),
        artifact_storage_path=getattr(review, 'artifact_storage_path', None),
        skipped_archive_count=int(getattr(review, 'skipped_archive_count', 0) or 0),
        error_message=getattr(review, 'error_message', None),
        report_header=REPORT_HEADER,
        performance_disclaimer=PERFORMANCE_DISCLAIMER,
        cco_revision_id=cco_revision_id,
        cco_policy_name=cco_policy_name,
    )
