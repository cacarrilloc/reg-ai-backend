import asyncio
import re
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import BackgroundTasks, HTTPException, UploadFile, status
from sqlalchemy import func, or_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import embedding_model_label, optional_embedding_provider
from app.core.config import Settings, get_settings
from app.core.observability import langfuse_workflow_trace
from app.core.security import CurrentUser
from app.db.ordering import desc
from app.models.core import (
    AuditLog,
    ComplianceHistory,
    DocumentChunk,
    GapAnalysis,
    Policy,
    PolicyRevision,
    Regulation,
)
from app.schemas.policies import (
    PolicyFileDownloadResponse,
    PolicyNameCheckResponse,
    PolicyNameSuggestion,
    PolicySummary,
    PolicyUploadResponse,
)
from app.services.document_chunk_service import (
    chunk_document_type_value,
    chunk_policy_name_value,
    delete_chunks_for_policy_ids,
    purge_superseded_policy_chunks,
)
from app.services.document_ingestion import (
    TextChunk,
    canonical_document_content_type,
    chunk_policy_document,
    generate_embeddings,
    parse_policy_document,
    read_upload_bytes,
)
from app.services.policy_types import (
    INTERNAL_POLICY_TYPE,
    MARKETING_CONTENT_TYPE,
    REGULATORY_REFERENCE_TYPE,
    is_internal_policy_type,
)
from app.services.regulation_catalog import ensure_marketing_regulations
from app.services.storage_service import (
    StoredFile,
    create_download_url,
    download_file,
    parse_storage_uri,
    remove_file,
    storage_path,
    upload_file,
)

logger = structlog.get_logger(__name__)
APPROVED_POLICY_BUCKET = "approved-policies"


async def emit_policy_updated_event(
    policy_id: str,
    org_id: str,
    *,
    skip_pipeline: bool = False,
) -> bool:
    """Emit an Inngest event to re-evaluate a policy against recent triggers."""
    settings = get_settings()
    if not settings.inngest_async_dispatch_enabled:
        return False
    try:
        import inngest

        from app.core.inngest import create_inngest_client
    except ImportError:
        return False

    client = create_inngest_client(settings)
    try:
        await client.send(
            inngest.Event(
                name='policy.updated',
                data={
                    'policy_id': policy_id,
                    'org_id': org_id,
                    'skip_pipeline': skip_pipeline,
                },
            )
        )
        return True
    except Exception as exc:
        logger.warning('policy_updated_event_failed', error_type=type(exc).__name__)
        return False


async def queue_policy_upload(
    file: UploadFile,
    user: CurrentUser,
    db: Session | None,
    policy_name: str | None = None,
    background_tasks: BackgroundTasks | None = None,
    persist: bool = False,
) -> PolicyUploadResponse:
    """Persist a policy upload quickly; embeddings and gap analysis run in the background.

    Chunks are stored without vectors here. ``run_policy_upload_workflow`` scrapes
    FINRA/SEC/CFTC, refreshes those regulation embeddings, embeds the uploaded
    policy, then overwrites or creates the Policy Update gap (same persist rules
    as cron). Request Review is never auto-queued.
    """
    settings = get_settings()
    filename = safe_filename(file.filename or "uploaded-policy")

    if not policy_name or not policy_name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Policy name is required.",
        )
    normalized_policy_name = normalize_policy_name(policy_name)

    contents = await read_upload_bytes(file, settings)
    parsed = await asyncio.to_thread(
        parse_policy_document,
        filename,
        file.content_type,
        contents,
    )
    chunks = await asyncio.to_thread(chunk_policy_document, parsed)
    policy_id = str(uuid4())
    persisted = False
    file_url: str | None = None
    resolved_policy_name = normalized_policy_name
    version = 1
    # Embeddings are generated asynchronously in the upload workflow.
    embeddings: list[list[float]] = []
    embedded_chunk_count = 0

    async with langfuse_workflow_trace(
        'policy-upload',
        session_id=policy_id,
        user_id=user.org_id,
        tags=['policy-upload', 'ingest'],
        metadata={'filename': filename, 'policy_name': normalized_policy_name},
        input_data={'filename': filename, 'chunk_count': len(chunks)},
    ):
        from app.db.session import SessionLocal, session_scope_or_existing

        stored_file: StoredFile | None = None
        if db is not None or (persist and SessionLocal is not None):
            stored_file = store_policy_file(
                org_id=user.org_id,
                policy_id=policy_id,
                filename=filename,
                contents=contents,
                content_type=canonical_document_content_type(parsed.document_type),
            )
            file_url = stored_file.uri

        with session_scope_or_existing(
            db, org_id=user.org_id, commit=False, open_if_missing=persist
        ) as session:
            if session is not None and stored_file is not None:
                resolved_policy_name = resolve_canonical_policy_name(
                    session,
                    user.org_id,
                    normalized_policy_name,
                )
                version = next_policy_version(session, user.org_id, resolved_policy_name)
                try:
                    persist_policy_upload(
                        db=session,
                        policy_id=policy_id,
                        org_id=user.org_id,
                        policy_name=resolved_policy_name,
                        version=version,
                        file_url=stored_file.uri,
                        chunks=chunks,
                        embeddings=embeddings,
                        embedding_model=embedding_model_label(user.org_id, settings),
                    )
                except HTTPException:
                    remove_file(stored_file)
                    raise
                persisted = True
                from app.services.review_service import mark_approved_revisions_reuploaded

                mark_approved_revisions_reuploaded(
                    session,
                    org_id=user.org_id,
                    policy_name=resolved_policy_name,
                )
                # Always analyze. Clearing UPLOAD_PENDING still overwrites the
                # Policy Update gap; Business Change rows stay beside it.

        if persisted:
            from app.services.workflow_dispatch_service import (
                queue_background_task,
                run_policy_upload_workflow_background,
            )

            inngest_delivered = await emit_policy_updated_event(
                policy_id,
                user.org_id,
                skip_pipeline=False,
            )
            workflow_queued = queue_background_task(
                background_tasks,
                run_policy_upload_workflow_background,
                policy_id,
                user.org_id,
                False,
            )
            if not workflow_queued and inngest_delivered:
                workflow_queued = True
            if not workflow_queued:
                logger.warning(
                    'policy_upload_workflow_not_queued',
                    policy_id=policy_id,
                    org_id=user.org_id,
                    reason='inngest_unavailable_and_no_background_tasks',
                )
            else:
                logger.info(
                    'policy_upload_persisted_embeddings_deferred',
                    policy_id=policy_id,
                    org_id=user.org_id,
                    chunk_count=len(chunks),
                )

    return PolicyUploadResponse(
        policy_id=policy_id,
        org_id=user.org_id,
        filename=filename,
        status=upload_status(persisted, len(chunks), embedded_chunk_count),
        chunk_count=len(chunks),
        embedded_chunk_count=embedded_chunk_count,
        embedding_model=embedding_model_label(user.org_id, settings),
        file_type=parsed.document_type,
        persisted=persisted,
        version=version,
        file_url=file_url,
    )


def policy_name_key(value: str) -> str:
    """Normalize a policy name for duplicate detection without changing its display casing."""
    return normalize_policy_name(value).casefold()


def _internal_policy_versions_for_name(
    db: Session,
    org_id: str,
    policy_name: str,
) -> list[Policy]:
    """Return every Internal Policy row that is a version of this name."""
    key = policy_name_key(policy_name)
    if not key:
        return []
    rows = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
            Policy.type != MARKETING_CONTENT_TYPE,
        )
        .all()
    )
    matched = [row for row in rows if policy_name_key(row.name) == key]
    return matched


def resolve_canonical_policy_name(
    db: Session | None,
    org_id: str,
    policy_name: str,
) -> str:
    """Reuse an existing internal policy's display name for case-insensitive exact matches."""
    if db is None:
        return policy_name
    existing = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
            Policy.type != MARKETING_CONTENT_TYPE,
            func.lower(Policy.name) == policy_name.lower(),
        )
        .order_by(desc(Policy.version), desc(Policy.updated_at))
        .first()
    )
    return existing.name if existing is not None else policy_name


async def check_policy_name(
    org_id: str,
    policy_name: str,
    db: Session | None,
    *,
    limit: int = 5,
) -> PolicyNameCheckResponse:
    """Find an exact internal policy name or suggest close existing names."""
    normalized_name = normalize_policy_name(policy_name)
    if db is None:
        return PolicyNameCheckResponse(normalized_name=normalized_name)

    policies = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
            Policy.type != MARKETING_CONTENT_TYPE,
        )
        .order_by(Policy.name.asc(), desc(Policy.version), desc(Policy.updated_at))
        .all()
    )
    latest_by_key: dict[str, Policy] = {}
    version_counts: dict[str, int] = {}
    for policy in policies:
        key = policy_name_key(policy.name)
        version_counts[key] = version_counts.get(key, 0) + 1
        current = latest_by_key.get(key)
        if current is None or policy.version > current.version:
            latest_by_key[key] = policy

    requested_key = policy_name_key(normalized_name)
    exact = latest_by_key.get(requested_key)
    if exact is not None:
        return PolicyNameCheckResponse(
            normalized_name=normalized_name,
            exact_match=PolicySummary(
                id=exact.id,
                org_id=exact.org_id,
                name=exact.name,
                type=exact.type,
                version=exact.version,
                status=exact.status,
                file_url=exact.file_url,
                created_at=exact.created_at,
                updated_at=exact.updated_at,
                version_count=version_counts[requested_key],
            ),
        )

    suggestions: list[PolicyNameSuggestion] = []
    for key, policy in latest_by_key.items():
        similarity = SequenceMatcher(None, requested_key, key).ratio()
        if similarity < 0.5:
            continue
        suggestions.append(
            PolicyNameSuggestion(
                id=policy.id,
                name=policy.name,
                version=policy.version,
                similarity=round(similarity, 3),
            )
        )
    suggestions.sort(key=lambda item: (-item.similarity, item.name.casefold()))
    return PolicyNameCheckResponse(
        normalized_name=normalized_name,
        similar_names=suggestions[:limit],
    )


def _policy_summary(row: Policy | Regulation, *, version_count: int) -> PolicySummary:
    return PolicySummary(
        id=row.id,
        org_id=row.org_id,
        name=row.name,
        type=row.type,
        version=row.version,
        status=row.status,
        file_url=row.file_url,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version_count=version_count,
    )


async def list_policies(org_id: str, db: Session | None) -> list[PolicySummary]:
    if db is None:
        return []

    ensure_marketing_regulations(db, org_id)
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()

    missing_marketing_files = (
        db.query(Regulation.id)
        .filter(
            Regulation.org_id == org_id,
            Regulation.rule_code.is_not(None),
            or_(Regulation.file_url.is_(None), Regulation.file_url == ''),
        )
        .limit(1)
        .first()
        is not None
    )
    if missing_marketing_files:
        from app.services.marketing_regulation_sync import schedule_marketing_regulation_sync

        schedule_marketing_regulation_sync(org_id)

    policies: list[Policy] = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type != MARKETING_CONTENT_TYPE,
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .order_by(Policy.name.asc(), desc(Policy.version), desc(Policy.updated_at))
        .all()
    )
    regulations: list[Regulation] = (
        db.query(Regulation)
        .filter(Regulation.org_id == org_id)
        .order_by(Regulation.name.asc(), desc(Regulation.version), desc(Regulation.updated_at))
        .all()
    )
    library_rows: list[Policy | Regulation] = []
    library_rows.extend(policies)
    library_rows.extend(regulations)
    version_counts: dict[str, int] = {}
    latest_rows: list[Policy | Regulation] = []
    for row in library_rows:
        version_counts[row.name] = version_counts.get(row.name, 0) + 1
        if version_counts[row.name] == 1:
            latest_rows.append(row)

    latest_rows.sort(key=lambda item: item.name.lower())
    return [_policy_summary(row, version_count=version_counts[row.name]) for row in latest_rows]


async def list_policy_versions(
    policy_id: str,
    org_id: str,
    db: Session | None,
) -> list[PolicySummary]:
    if db is None:
        return []

    policy = db.query(Policy).filter(Policy.id == policy_id, Policy.org_id == org_id).one_or_none()
    regulation = None if policy is not None else (
        db.query(Regulation).filter(Regulation.id == policy_id, Regulation.org_id == org_id).one_or_none()
    )
    if policy is None and regulation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

    if regulation is not None:
        regulation_versions: list[Regulation] = (
            db.query(Regulation)
            .filter(Regulation.org_id == org_id, Regulation.name == regulation.name)
            .order_by(desc(Regulation.version))
            .all()
        )
        version_count = len(regulation_versions)
        return [_policy_summary(item, version_count=version_count) for item in regulation_versions]

    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

    policy_versions: list[Policy] = (
        db.query(Policy)
        .filter(Policy.org_id == org_id, Policy.name == policy.name)
        .order_by(desc(Policy.version))
        .all()
    )
    version_count = len(policy_versions)
    return [_policy_summary(item, version_count=version_count) for item in policy_versions]


def delete_internal_policy_for_org(
    db: Session | None,
    org_id: str,
    policy_id: str,
) -> bool:
    """Delete one Internal Policy name (all versions) and its gap/review rows.

    System Regulations and marketing content rows cannot be deleted here.
    Compliance history keeps the incident with ``policy_id`` / ``gap_analysis_id``
    set null. Audit logs stay append-only with those FKs cleared.
    """
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Policy delete requires a configured database',
        )

    policy = (
        db.query(Policy)
        .filter(Policy.id == policy_id, Policy.org_id == org_id)
        .one_or_none()
    )
    if policy is None:
        return False
    if not is_internal_policy_type(policy.type):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Only Internal Policy documents can be deleted from this table.',
        )

    policy_name = policy.name
    versions = _internal_policy_versions_for_name(db, org_id, policy_name)
    if policy.id not in {row.id for row in versions}:
        versions = [policy, *versions]
    policy_ids = [row.id for row in versions if row.id]
    file_urls = [row.file_url for row in versions if getattr(row, 'file_url', None)]
    chunk_count = 0
    gap_ids: list[str] = []

    try:
        gaps = (
            db.query(GapAnalysis)
            .filter(GapAnalysis.org_id == org_id, GapAnalysis.policy_id.in_(policy_ids))
            .all()
        )
        gap_ids = [gap.id for gap in gaps]

        if gap_ids:
            (
                db.query(AuditLog)
                .filter(AuditLog.org_id == org_id, AuditLog.gap_analysis_id.in_(gap_ids))
                .update(
                    {AuditLog.revision_id: None, AuditLog.gap_analysis_id: None},
                    synchronize_session=False,
                )
            )
            (
                db.query(ComplianceHistory)
                .filter(
                    ComplianceHistory.org_id == org_id,
                    ComplianceHistory.gap_analysis_id.in_(gap_ids),
                )
                .update(
                    {ComplianceHistory.gap_analysis_id: None},
                    synchronize_session=False,
                )
            )
            (
                db.query(PolicyRevision)
                .filter(
                    PolicyRevision.org_id == org_id,
                    PolicyRevision.gap_analysis_id.in_(gap_ids),
                )
                .delete(synchronize_session=False)
            )
            (
                db.query(GapAnalysis)
                .filter(GapAnalysis.org_id == org_id, GapAnalysis.id.in_(gap_ids))
                .delete(synchronize_session=False)
            )

        (
            db.query(AuditLog)
            .filter(AuditLog.org_id == org_id, AuditLog.policy_id.in_(policy_ids))
            .update(
                {AuditLog.policy_id: None, AuditLog.revision_id: None},
                synchronize_session=False,
            )
        )
        (
            db.query(ComplianceHistory)
            .filter(
                ComplianceHistory.org_id == org_id,
                ComplianceHistory.policy_id.in_(policy_ids),
            )
            .update(
                {ComplianceHistory.policy_id: None},
                synchronize_session=False,
            )
        )
        (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.org_id == org_id,
                PolicyRevision.policy_id.in_(policy_ids),
            )
            .delete(synchronize_session=False)
        )
        chunk_count = delete_chunks_for_policy_ids(db, policy_ids)
        (
            db.query(Policy)
            .filter(Policy.org_id == org_id, Policy.id.in_(policy_ids))
            .delete(synchronize_session=False)
        )
        leftover = _internal_policy_versions_for_name(db, org_id, policy_name)
        leftover_ids = [row.id for row in leftover if row.id]
        if leftover_ids:
            leftover_urls = [row.file_url for row in leftover if getattr(row, 'file_url', None)]
            file_urls.extend(leftover_urls)
            policy_ids.extend(leftover_ids)
            delete_chunks_for_policy_ids(db, leftover_ids)
            (
                db.query(Policy)
                .filter(Policy.org_id == org_id, Policy.id.in_(leftover_ids))
                .delete(synchronize_session=False)
            )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'internal_policy_delete_failed',
            org_id=org_id,
            policy_id=policy_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to delete internal policy',
        ) from exc

    for file_url in file_urls:
        try:
            remove_file(parse_storage_uri(str(file_url)))
        except Exception:
            continue

    logger.info(
        'internal_policy_deleted',
        org_id=org_id,
        policy_id=policy_id,
        policy_name=policy_name,
        versions_deleted=len(policy_ids),
        cascaded_gaps=len(gap_ids),
        cascaded_chunks=chunk_count,
    )
    return True


async def get_policy_download_url(
    policy_id: str,
    org_id: str,
    db: Session | None,
) -> PolicyFileDownloadResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Policy storage is unavailable without a configured database",
        )

    record: Policy | Regulation | None = (
        db.query(Policy).filter(Policy.id == policy_id, Policy.org_id == org_id).one_or_none()
    )
    if record is None:
        record = db.query(Regulation).filter(
            Regulation.id == policy_id, Regulation.org_id == org_id
        ).one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")
    if not record.file_url:
        rule_code = str(getattr(record, 'rule_code', None) or '').strip()
        if rule_code:
            from app.core.config import get_settings
            from app.services.marketing_regulation_sync import sync_marketing_regulation_documents

            await sync_marketing_regulation_documents(
                org_id, get_settings(), rule_codes={rule_code}
            )
            db.expire_all()
            refreshed = (
                db.query(Regulation)
                .filter(Regulation.id == policy_id, Regulation.org_id == org_id)
                .one_or_none()
            )
            if refreshed is not None:
                record = refreshed
        if not getattr(record, 'file_url', None):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy file is unavailable")

    if record.file_url.startswith(('http://', 'https://')):
        return PolicyFileDownloadResponse(url=record.file_url, expires_in=0)

    expires_in = 900
    return PolicyFileDownloadResponse(
        url=create_download_url(record.file_url, expires_in),
        expires_in=expires_in,
    )


def persist_policy_upload(
    db: Session,
    policy_id: str,
    org_id: str,
    policy_name: str,
    version: int,
    file_url: str,
    chunks: list[TextChunk],
    embeddings: list[list[float]],
    embedding_model: str,
) -> None:
    policy = Policy(
        id=policy_id,
        org_id=org_id,
        name=policy_name,
        type=INTERNAL_POLICY_TYPE,
        version=version,
        status="ACTIVE",
        file_url=file_url,
    )
    try:
        db.query(Policy).filter(
            Policy.org_id == org_id,
            Policy.name == policy_name,
            Policy.status == "ACTIVE",
        ).update({Policy.status: "SUPERSEDED"}, synchronize_session=False)
        # Drop embeddings for policies we just superseded (RAG uses ACTIVE only).
        purge_superseded_policy_chunks(db, org_id=org_id)
        db.add(policy)
        db.flush()

        for chunk in chunks:
            embedding = embeddings[chunk.index] if embeddings else None
            db.add(
                DocumentChunk(
                    id=str(uuid4()),
                    org_id=org_id,
                    policy_id=policy_id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    document_type=chunk_document_type_value(policy_type=INTERNAL_POLICY_TYPE),
                    policy_name=chunk_policy_name_value(
                        policy_name=policy_name,
                        policy_type=INTERNAL_POLICY_TYPE,
                    ),
                    embedding=embedding,
                    metadata_json={
                        **chunk.metadata,
                        "policy_type": INTERNAL_POLICY_TYPE,
                        "embedding_model": embedding_model if embedding is not None else None,
                        "embedded": embedding is not None,
                    },
                )
            )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "policy_persistence_failed",
            policy_id=policy_id,
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        detail = "Unable to persist policy"
        if not get_settings().is_production:
            detail = f"{detail}: {getattr(exc, 'orig', exc)}"
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        ) from exc


async def backfill_policy_embeddings(
    db: Session,
    policy_id: str,
    org_id: str,
    settings: Settings | None = None,
) -> int:
    """Embed any persisted policy chunks that are still missing pgvector rows.

    Prefer ``backfill_policy_embeddings_releasing`` so OpenAI calls do not hold
    a pooled connection.
    """
    resolved_settings = settings or get_settings()
    if optional_embedding_provider(org_id) is None:
        return 0

    snapshots = _missing_policy_chunk_snapshots(db, policy_id, org_id)
    if snapshots is None:
        return 0
    if not snapshots:
        return 0

    from app.services.document_ingestion import TextChunk, generate_embeddings

    text_chunks = [
        TextChunk(
            index=int(str(item['chunk_index'])),
            content=str(item['content']),
            metadata=(
                dict(item['metadata_json'])
                if isinstance(item['metadata_json'], dict)
                else {}
            ),
        )
        for item in snapshots
    ]
    embeddings = await generate_embeddings(text_chunks, resolved_settings, org_id=org_id)
    if len(embeddings) != len(text_chunks):
        logger.warning(
            'policy_embedding_backfill_incomplete',
            policy_id=policy_id,
            org_id=org_id,
            expected=len(text_chunks),
            received=len(embeddings),
        )
        return 0
    return _apply_policy_chunk_embeddings(
        db,
        policy_id=policy_id,
        org_id=org_id,
        snapshots=snapshots,
        embeddings=embeddings,
        settings=resolved_settings,
    )


async def backfill_policy_embeddings_releasing(
    policy_id: str,
    org_id: str,
    settings: Settings | None = None,
) -> int:
    """Load missing chunks → embed offline → write embeddings (no long DB hold)."""
    from app.db.session import session_scope
    from app.services.document_ingestion import TextChunk, generate_embeddings

    resolved_settings = settings or get_settings()
    if optional_embedding_provider(org_id) is None:
        return 0

    with session_scope(commit=False, org_id=org_id) as scoped:
        snapshots = _missing_policy_chunk_snapshots(scoped, policy_id, org_id)
    if snapshots is None or not snapshots:
        return 0

    text_chunks = [
        TextChunk(
            index=int(str(item['chunk_index'])),
            content=str(item['content']),
            metadata=(
                dict(item['metadata_json'])
                if isinstance(item['metadata_json'], dict)
                else {}
            ),
        )
        for item in snapshots
    ]
    embeddings = await generate_embeddings(text_chunks, resolved_settings, org_id=org_id)
    if len(embeddings) != len(text_chunks):
        logger.warning(
            'policy_embedding_backfill_incomplete',
            policy_id=policy_id,
            org_id=org_id,
            expected=len(text_chunks),
            received=len(embeddings),
        )
        return 0

    with session_scope(org_id=org_id) as scoped:
        return _apply_policy_chunk_embeddings(
            scoped,
            policy_id=policy_id,
            org_id=org_id,
            snapshots=snapshots,
            embeddings=embeddings,
            settings=resolved_settings,
        )


def _missing_policy_chunk_snapshots(
    db: Session,
    policy_id: str,
    org_id: str,
) -> list[dict[str, object]] | None:
    policy = (
        db.query(Policy)
        .filter(Policy.id == policy_id, Policy.org_id == org_id, Policy.status == 'ACTIVE')
        .one_or_none()
    )
    if policy is None:
        return None

    missing_chunks = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.policy_id == policy_id,
            DocumentChunk.org_id == org_id,
            DocumentChunk.embedding.is_(None),
        )
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    return [
        {
            'id': chunk.id,
            'chunk_index': chunk.chunk_index,
            'content': chunk.content,
            'metadata_json': dict(chunk.metadata_json or {}),
        }
        for chunk in missing_chunks
    ]


def _apply_policy_chunk_embeddings(
    db: Session,
    *,
    policy_id: str,
    org_id: str,
    snapshots: list[dict[str, object]],
    embeddings: list[list[float]],
    settings: Settings,
) -> int:
    chunk_ids = [str(item['id']) for item in snapshots]
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.id.in_(chunk_ids))
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    by_id = {chunk.id: chunk for chunk in chunks}
    model_label = embedding_model_label(org_id, settings)
    embedded = 0
    for snapshot, embedding in zip(snapshots, embeddings, strict=True):
        chunk = by_id.get(str(snapshot['id']))
        if chunk is None:
            continue
        chunk.embedding = embedding
        metadata = dict(chunk.metadata_json or {})
        metadata['embedded'] = True
        metadata['embedding_model'] = model_label
        chunk.metadata_json = metadata
        embedded += 1
    db.commit()
    logger.info(
        'policy_embedding_backfill_completed',
        policy_id=policy_id,
        org_id=org_id,
        embedded_chunks=embedded,
    )
    return embedded


def next_policy_version(db: Session, org_id: str, policy_name: str) -> int:
    max_version = (
        db.query(func.max(Policy.version)).filter(Policy.org_id == org_id, Policy.name == policy_name).scalar()
    )
    return int(max_version or 0) + 1


async def increment_policy_version(
    policy_id: str,
    org_id: str,
    redline_file_url: str,
    db: Session,
    *,
    approved_bytes: bytes | None = None,
) -> tuple[int, str]:
    """Increment policy version after CCO approval.

    Args:
        policy_id: ID of the policy being versioned
        org_id: Organization ID
        redline_file_url: URL of the approved redline document (fallback download source)
        db: Database session
        approved_bytes: Optional pre-accepted clean DOCX bytes. When omitted, downloads
            ``redline_file_url``.

    Returns:
        New version number and storage URI
    """
    policy: Policy | None = (
        db.query(Policy)
        .filter(
            Policy.id == policy_id,
            Policy.org_id == org_id,
        )
        .one_or_none()
    )

    if not policy:
        raise ValueError(f"Policy {policy_id} not found")

    document_bytes = approved_bytes if approved_bytes is not None else download_file(redline_file_url)
    new_version = next_policy_version(db, org_id, policy.name)
    new_policy_id = str(uuid4())

    parsed = parse_policy_document(
        f"{policy.name}.docx",
        canonical_document_content_type("docx"),
        document_bytes,
    )
    chunks = chunk_policy_document(parsed)
    settings = get_settings()
    embeddings = await generate_embeddings(chunks, settings, org_id=org_id)
    model_label = embedding_model_label(org_id, settings)
    # Store in the Policy Library bucket (same path as manual uploads).
    approved_file = store_policy_file(
        org_id=org_id,
        policy_id=new_policy_id,
        filename=approved_policy_filename(policy, new_version),
        contents=document_bytes,
        content_type=canonical_document_content_type("docx"),
    )

    new_policy = Policy(
        id=new_policy_id,
        org_id=org_id,
        name=policy.name,
        type=policy.type,
        version=new_version,
        status="ACTIVE",
        file_url=approved_file.uri,
    )

    try:
        # Mark previous version as superseded and drop its unused embeddings.
        superseded_policy_id = policy.id
        policy.status = "SUPERSEDED"
        delete_chunks_for_policy_ids(db, [superseded_policy_id])

        # Add new version
        db.add(new_policy)
        db.flush()

        for chunk in chunks:
            embedding = embeddings[chunk.index] if embeddings else None
            db.add(
                DocumentChunk(
                    id=str(uuid4()),
                    org_id=org_id,
                    policy_id=new_policy_id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    document_type=chunk_document_type_value(policy_type=policy.type),
                    policy_name=chunk_policy_name_value(
                        policy_name=policy.name,
                        policy_type=policy.type,
                    ),
                    embedding=embedding,
                    metadata_json={
                        **chunk.metadata,
                        "policy_type": policy.type,
                        "embedding_model": model_label if embedding is not None else None,
                        "embedded": embedding is not None,
                    },
                )
            )

        db.commit()

        logger.info(
            "policy_version_created",
            policy_id=new_policy_id,
            org_id=org_id,
            version=new_version,
            redline_file_url=approved_file.uri,
        )

        return new_version, approved_file.uri
    except SQLAlchemyError as exc:
        db.rollback()
        remove_file(approved_file)
        logger.error("failed_to_increment_policy_version", error=str(exc))
        raise


async def store_approved_policy_version(
    policy: Policy,
    new_policy_id: str,
    version: int,
    approved_bytes: bytes,
) -> StoredFile:
    """Preserve a newly approved redline as the canonical policy document copy."""
    return upload_file(
        APPROVED_POLICY_BUCKET,
        storage_path(
            policy.org_id,
            "approved-policies",
            new_policy_id,
            approved_policy_filename(policy, version),
        ),
        approved_bytes,
        canonical_document_content_type("docx"),
    )


def upload_status(persisted: bool, chunk_count: int, embedded_chunk_count: int) -> str:
    if chunk_count and embedded_chunk_count == chunk_count:
        return "ingested" if persisted else "embedded_not_persisted"
    return "parsed_without_embeddings"


def policy_name_from_filename(filename: str) -> str:
    stem = Path(filename).stem.strip()
    name = re.sub(r"[_-]+", " ", stem).strip()
    return name or "Uploaded policy"


def normalize_policy_name(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Policy name cannot be empty",
        )
    return normalized[:180]


def safe_filename(filename: str) -> str:
    basename = re.split(r"[/\\]", filename)[-1].strip()
    return basename[:180] or "uploaded-policy"


def store_policy_file(
    org_id: str,
    policy_id: str,
    filename: str,
    contents: bytes,
    content_type: str | None,
) -> StoredFile:
    settings = get_settings()
    path = storage_path(org_id, "policies", policy_id, filename)
    return upload_file(settings.supabase_storage_bucket, path, contents, content_type)


def approved_policy_filename(policy: Policy, version: int) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", policy.name).strip("._") or "policy"
    return f"{base}_v{version}_approved.docx"
