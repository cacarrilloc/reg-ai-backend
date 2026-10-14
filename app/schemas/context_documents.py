from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ContextDocumentType(StrEnum):
    PRIOR_EXAM_RESPONSE = 'PRIOR_EXAM_RESPONSE'
    REPORTING_HISTORY = 'REPORTING_HISTORY'
    VIOLATION_HISTORY = 'VIOLATION_HISTORY'


class ContextDocumentUploadResponse(BaseModel):
    context_document_id: str
    org_id: str
    filename: str
    document_type: ContextDocumentType
    status: str
    chunk_count: int = 0
    embedded_chunk_count: int = 0
    embedding_model: str | None = None
    file_type: str | None = None
    persisted: bool = False
    file_url: str | None = None


class ContextDocumentSummary(BaseModel):
    id: str
    org_id: str
    document_type: ContextDocumentType
    title: str
    file_url: str | None = None
    created_at: datetime | None = None
