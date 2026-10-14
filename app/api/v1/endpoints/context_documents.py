from fastapi import APIRouter, Depends, File, UploadFile, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user
from app.db.session import get_db
from app.schemas.context_documents import (
    ContextDocumentSummary,
    ContextDocumentType,
    ContextDocumentUploadResponse,
)
from app.schemas.policies import PolicyFileDownloadResponse
from app.services.context_document_service import (
    get_context_document_download_url,
    list_context_documents,
    upload_context_document,
)

router = APIRouter(prefix='/context-documents', tags=['context-documents'])


@router.get('', response_model=list[ContextDocumentSummary])
async def get_context_documents(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> list[ContextDocumentSummary]:
    return await list_context_documents(user.org_id, db)


@router.get('/{document_id}/download-url', response_model=PolicyFileDownloadResponse)
async def get_context_document_file_download_url(
    document_id: str,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> PolicyFileDownloadResponse:
    return await get_context_document_download_url(document_id, user.org_id, db)


@router.post(
    '/{document_type}/upload',
    response_model=ContextDocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_context_document_file(
    document_type: ContextDocumentType,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(get_current_user),
) -> ContextDocumentUploadResponse:
    return await upload_context_document(file, document_type, user, None, persist=True)
