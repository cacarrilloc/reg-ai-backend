from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import structlog
from fastapi import HTTPException, UploadFile, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import embedding_model_label
from app.core.config import get_settings
from app.core.security import CurrentUser
from app.db.ordering import desc
from app.models.core import ContextDocument, DocumentChunk
from app.schemas.context_documents import (
    ContextDocumentSummary,
    ContextDocumentType,
    ContextDocumentUploadResponse,
)
from app.schemas.policies import PolicyFileDownloadResponse
from app.services.document_chunk_service import chunk_document_type_value, chunk_policy_name_value
from app.services.document_ingestion import (
    TextChunk,
    canonical_document_content_type,
    chunk_policy_document,
    generate_embeddings,
    parse_policy_document,
    read_upload_bytes,
)
from app.services.policy_service import safe_filename, upload_status
from app.services.storage_service import (
    StoredFile,
    create_download_url,
    remove_file,
    storage_path,
    upload_file,
)

logger = structlog.get_logger(__name__)
CONTEXT_DOCUMENT_BUCKET = 'context-documents'


async def upload_context_document(
    file: UploadFile,
    document_type: ContextDocumentType,
    user: CurrentUser,
    db: Session | None,
    *,
    persist: bool = False,
) -> ContextDocumentUploadResponse:
    settings = get_settings()
    filename = safe_filename(file.filename or 'uploaded-context-document')
    contents = await read_upload_bytes(file, settings)
    parsed = await asyncio.to_thread(parse_policy_document, filename, file.content_type, contents)
    chunks = await asyncio.to_thread(chunk_policy_document, parsed)
    embeddings = await generate_embeddings(chunks, settings, org_id=user.org_id)

    if embeddings and len(embeddings) != len(chunks):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail='OpenAI returned an unexpected embedding count during context ingestion',
        )

    context_document_id = str(uuid4())
    file_url: str | None = None
    persisted = False
    from app.db.session import SessionLocal, session_scope_or_existing

    stored_file: StoredFile | None = None
    if db is not None or (persist and SessionLocal is not None):
        stored_file = store_context_document_file(
            org_id=user.org_id,
            context_document_id=context_document_id,
            filename=filename,
            contents=contents,
            content_type=canonical_document_content_type(parsed.document_type),
        )
        file_url = stored_file.uri

    with session_scope_or_existing(
        db, org_id=user.org_id, commit=False, open_if_missing=persist
    ) as session:
        if session is not None and stored_file is not None:
            try:
                persist_context_document(
                    db=session,
                    context_document_id=context_document_id,
                    org_id=user.org_id,
                    document_type=document_type,
                    title=Path(filename).stem or filename,
                    file_url=file_url or stored_file.uri,
                    chunks=chunks,
                    embeddings=embeddings,
                    embedding_model=embedding_model_label(user.org_id, settings),
                )
            except HTTPException:
                remove_file(stored_file)
                raise
            persisted = True

    return ContextDocumentUploadResponse(
        context_document_id=context_document_id,
        org_id=user.org_id,
        filename=filename,
        document_type=document_type,
        status=upload_status(persisted, len(chunks), len(embeddings)),
        chunk_count=len(chunks),
        embedded_chunk_count=len(embeddings),
        embedding_model=embedding_model_label(user.org_id, settings),
        file_type=parsed.document_type,
        persisted=persisted,
        file_url=file_url,
    )


async def list_context_documents(
    org_id: str,
    db: Session | None,
) -> list[ContextDocumentSummary]:
    if db is None:
        return []

    documents = db.query(ContextDocument).filter(
        ContextDocument.org_id == org_id).order_by(desc(ContextDocument.created_at)).all()
    return [
        ContextDocumentSummary(
            id=document.id,
            org_id=document.org_id,
            document_type=ContextDocumentType(document.document_type),
            title=document.title,
            file_url=document.file_url,
            created_at=document.created_at,
        )
        for document in documents
    ]


async def get_context_document_download_url(
    document_id: str,
    org_id: str,
    db: Session | None,
) -> PolicyFileDownloadResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Context document storage is unavailable without a configured database',
        )

    document = (
        db.query(ContextDocument)
        .filter(ContextDocument.id == document_id, ContextDocument.org_id == org_id)
        .one_or_none()
    )
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Context document not found')
    if not document.file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Context document file is unavailable',
        )

    if document.file_url.startswith(('http://', 'https://')):
        return PolicyFileDownloadResponse(url=document.file_url, expires_in=0)

    expires_in = 900
    return PolicyFileDownloadResponse(
        url=create_download_url(document.file_url, expires_in),
        expires_in=expires_in,
    )


def persist_context_document(
    db: Session,
    context_document_id: str,
    org_id: str,
    document_type: ContextDocumentType,
    title: str,
    file_url: str,
    chunks: list[TextChunk],
    embeddings: list[list[float]],
    embedding_model: str,
) -> None:
    document = ContextDocument(
        id=context_document_id,
        org_id=org_id,
        document_type=document_type.value,
        title=title,
        file_url=file_url,
        metadata_json={
            'embedding_model': embedding_model if embeddings else None,
            'embedded_chunk_count': len(embeddings),
        },
    )
    try:
        db.add(document)
        db.flush()
        for chunk in chunks:
            embedding = embeddings[chunk.index] if embeddings else None
            db.add(
                DocumentChunk(
                    id=str(uuid4()),
                    org_id=org_id,
                    context_document_id=context_document_id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    document_type=chunk_document_type_value(context_document=True),
                    policy_name=chunk_policy_name_value(policy_name=document.title),
                    embedding=embedding,
                    metadata_json={
                        **chunk.metadata,
                        'embedding_model': embedding_model if embedding is not None else None,
                        'embedded': embedding is not None,
                    },
                )
            )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'context_document_persistence_failed',
            context_document_id=context_document_id,
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to persist context document',
        ) from exc


def store_context_document_file(
    org_id: str,
    context_document_id: str,
    filename: str,
    contents: bytes,
    content_type: str | None,
) -> StoredFile:
    settings = get_settings()
    if CONTEXT_DOCUMENT_BUCKET not in settings.supabase_storage_bucket_list:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='The context-documents Supabase Storage bucket is not configured',
        )
    path = storage_path(org_id, 'context', context_document_id, filename)
    return upload_file(CONTEXT_DOCUMENT_BUCKET, path, contents, content_type)
