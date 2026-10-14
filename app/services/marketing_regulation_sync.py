"""Scrape official SEC/FINRA marketing rules into ``regulations`` with DOCX + embeddings."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.orm import Session

from app.ai import embedding_model_label
from app.core.config import Settings, get_settings
from app.models.core import DocumentChunk, Regulation
from app.services.document_chunk_service import delete_chunks_for_regulation_ids
from app.services.document_ingestion import TextChunk, chunk_publication_text, generate_embeddings
from app.services.marketing_rule_corpus import (
    MARKETING_CATALOG_GROUPS,
    RULE_SPECS,
    RuleDocument,
    catalog_rule_documents,
    load_marketing_rule_corpus,
)
from app.services.regulation_catalog import ensure_marketing_regulations
from app.services.regulatory_reference_policy_service import (
    content_hash,
    persist_reference_chunks,
    store_reference_policy_file,
)

logger = structlog.get_logger(__name__)
_background_tasks: set[asyncio.Task[Any]] = set()
_refresh_guard = threading.Lock()
_refresh_orgs: set[str] = set()


def _catalog_codes() -> tuple[str, ...]:
    return tuple(item[0] for item in MARKETING_CATALOG_GROUPS)


def _already_current(db: Session, row: Regulation, digest: str) -> bool:
    if not (row.file_url or '').strip():
        return False
    chunk = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.regulation_id == row.id)
        .order_by(DocumentChunk.chunk_index.asc())
        .first()
    )
    if chunk is None or chunk.embedding is None:
        return False
    return str((chunk.metadata_json or {}).get('content_hash') or '') == digest


def load_stored_marketing_rule_documents(db: Session, org_id: str) -> list[RuleDocument]:
    """Rebuild catalog RuleDocuments from persisted regulation chunks."""
    rows = ensure_marketing_regulations(db, org_id)
    by_code = {str(row.rule_code): row for row in rows if row.rule_code}
    documents: list[RuleDocument] = []
    for catalog_id, regulator, title, _members in MARKETING_CATALOG_GROUPS:
        row = by_code.get(catalog_id)
        if row is None:
            return []
        chunks = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.regulation_id == row.id)
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )
        text = '\n\n'.join(chunk.content for chunk in chunks if chunk.content).strip()
        if len(text) < 80:
            return []
        documents.append(
            RuleDocument(
                rule_id=catalog_id,
                regulator=regulator,
                title=title,
                url=(row.file_url or '') if str(row.file_url or '').startswith('http') else '',
                text=text,
                source='stored',
            )
        )
    return documents


def _apply_marketing_rule(
    db: Session,
    *,
    org_id: str,
    row: Regulation,
    document: RuleDocument,
    digest: str,
    chunks: list[TextChunk],
    embeddings: list[list[float]],
    settings: Settings,
) -> Regulation:
    stored_file_url = store_reference_policy_file(
        org_id=org_id,
        policy_id=row.id,
        policy_name=document.title,
        document_text=document.text,
    )
    row.file_url = stored_file_url or document.url or row.file_url
    row.name = document.title
    row.source = document.regulator
    row.status = 'ACTIVE'
    row.updated_at = datetime.now(UTC).replace(tzinfo=None)
    delete_chunks_for_regulation_ids(db, [row.id])
    persist_reference_chunks(
        db=db,
        org_id=org_id,
        policy_id=row.id,
        chunks=chunks,
        embeddings=embeddings,
        embedding_model=embedding_model_label(org_id, settings),
        source=document.regulator,
        content_hash=digest,
    )
    db.flush()
    return row


async def sync_marketing_regulation_documents(
    org_id: str,
    settings: Settings | None = None,
    *,
    rule_codes: set[str] | None = None,
    force: bool = False,
) -> int:
    """Fetch official marketing-rule text, store DOCX, and embed into the org schema.

    ``force=True`` bypasses the in-memory fetch cache and re-embeds even when the
    stored content hash is unchanged. Used when a new Marketing Review starts.
    """
    settings = settings or get_settings()
    org_id = (org_id or '').strip()
    if not org_id:
        return 0
    if force:
        from app.services.marketing_rule_corpus import clear_rule_corpus_cache

        clear_rule_corpus_cache()

    wanted = {code.strip() for code in (rule_codes or _catalog_codes()) if code.strip()}
    member_ids = {
        member_id
        for catalog_id, _regulator, _title, members in MARKETING_CATALOG_GROUPS
        if catalog_id in wanted
        for member_id in members
    }
    specs = tuple(spec for spec in RULE_SPECS if spec.rule_id in member_ids)
    live_docs = [
        item
        for item in catalog_rule_documents(await load_marketing_rule_corpus(settings, specs=specs))
        if item.rule_id in wanted
    ]
    if not live_docs:
        return 0

    from app.db.session import session_scope

    planned: list[tuple[str, RuleDocument, str]] = []
    with session_scope(commit=True, org_id=org_id) as db:
        rows = ensure_marketing_regulations(db, org_id)
        by_code = {str(row.rule_code): row for row in rows if row.rule_code}
        for document in live_docs:
            row = by_code.get(document.rule_id)
            if row is None:
                continue
            digest = content_hash(document.text)
            if not force and _already_current(db, row, digest):
                continue
            planned.append((row.id, document, digest))

    updated = 0
    for regulation_id, document, digest in planned:
        chunks = chunk_publication_text(document.title, document.text)
        try:
            embeddings = await generate_embeddings(chunks, settings, org_id=org_id) if chunks else []
        except Exception as exc:
            logger.warning(
                'marketing_regulation_embed_failed',
                org_id=org_id,
                rule_code=document.rule_id,
                error_type=type(exc).__name__,
            )
            embeddings = []
        try:
            with session_scope(commit=True, org_id=org_id) as db:
                row = (
                    db.query(Regulation)
                    .filter(Regulation.id == regulation_id, Regulation.org_id == org_id)
                    .one_or_none()
                )
                if row is None:
                    continue
                _apply_marketing_rule(
                    db,
                    org_id=org_id,
                    row=row,
                    document=document,
                    digest=digest,
                    chunks=chunks,
                    embeddings=embeddings,
                    settings=settings,
                )
                updated += 1
            logger.info(
                'marketing_regulation_synced',
                org_id=org_id,
                rule_code=document.rule_id,
                source=document.source,
                downloadable=True,
            )
        except Exception as exc:
            logger.warning(
                'marketing_regulation_persist_failed',
                org_id=org_id,
                rule_code=document.rule_id,
                error_type=type(exc).__name__,
            )
    return updated


async def refresh_marketing_rules_for_review(
    org_id: str,
    settings: Settings | None = None,
) -> int:
    """Ensure marketing-rule embeddings exist for a new Marketing Review.

    Live scrape of official rule pages is skipped when stored docs already exist
    (that path exhausted the DB pool). The review workflow still re-embeds the
    submitted content and stored rule text on every request.
    """
    org_id = (org_id or '').strip()
    if not org_id:
        return 0
    with _refresh_guard:
        if org_id in _refresh_orgs:
            logger.info('marketing_review_rules_refresh_skipped_inflight', org_id=org_id)
            return 0
        _refresh_orgs.add(org_id)
    try:
        from app.db.session import session_scope

        with session_scope(commit=False, org_id=org_id) as db:
            stored = load_stored_marketing_rule_documents(db, org_id)
        from app.services.marketing_vector_gap import clear_embed_cache

        clear_embed_cache()
        if len(stored) >= 4:
            logger.info('marketing_review_rules_already_stored', org_id=org_id)
            return 0
        updated = await sync_marketing_regulation_documents(
            org_id,
            settings,
            force=False,
        )
        logger.info(
            'marketing_review_rules_refreshed',
            org_id=org_id,
            updated=updated,
        )
        return updated
    except Exception as exc:
        logger.warning(
            'marketing_review_rules_refresh_failed',
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        return 0
    finally:
        with _refresh_guard:
            _refresh_orgs.discard(org_id)


def schedule_marketing_regulation_sync(org_id: str) -> None:
    """Queue a non-blocking scrape/embed of the four marketing rules."""
    org_id = (org_id or '').strip()
    if not org_id:
        return

    async def _run() -> None:
        try:
            await sync_marketing_regulation_documents(org_id)
        except Exception as exc:
            logger.warning(
                'marketing_regulation_sync_background_failed',
                org_id=org_id,
                error_type=type(exc).__name__,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_run(), name=f'marketing-rule-sync-{org_id}')
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def resolve_marketing_rule_documents(
    org_id: str,
    settings: Settings | None = None,
) -> list[RuleDocument] | None:
    """Return stored catalog documents, scraping first when the org schema is empty."""
    settings = settings or get_settings()
    try:
        from app.db.session import session_scope

        with session_scope(commit=False, org_id=org_id) as db:
            stored = load_stored_marketing_rule_documents(db, org_id)
        if len(stored) >= 4:
            return stored
        await sync_marketing_regulation_documents(org_id, settings)
        with session_scope(commit=False, org_id=org_id) as db:
            stored = load_stored_marketing_rule_documents(db, org_id)
        return stored or None
    except Exception as exc:
        logger.warning(
            'marketing_regulation_resolve_failed',
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        return None
