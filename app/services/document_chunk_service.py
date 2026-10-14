"""Helpers for typed document chunk persistence and vector comparison."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.ai import embedding_model_label, optional_embedding_provider
from app.core.config import Settings, get_settings
from app.core.time import to_utc, utc_now
from app.db.pool_guards import background_db_slot
from app.db.session import session_scope
from app.models.core import DocumentChunk, GapAnalysis, Policy, PolicyRevision, Regulation
from app.services.document_ingestion import TextChunk
from app.services.policy_types import (
    INTERNAL_POLICY_TYPE,
    REDLINE_CHUNK_TYPE,
    REGULATORY_REFERENCE_TYPE,
    chunk_document_type_for_policy,
    normalize_chunk_policy_name,
    resolve_internal_regulator_source,
)
from app.services.redline_vector_validation_service import mean_max_cosine_similarity

logger = structlog.get_logger(__name__)

REGULATOR_SOURCES = ('SEC', 'FINRA', 'CFTC')
# Relatedness floor for every embedding comparison in the product (retrieval,
# marketing, business-change matching, and redline quality gate).
COSINE_RELATEDNESS_FLOOR = 0.25

REDLINE_CHUNK_INDEX_BASE = 10_000_000
REDLINE_CHUNK_INDEX_STRIDE = 1_000
MAX_REDLINE_SIMILARITY_BACKFILL_ATTEMPTS = 3
REDLINE_SIMILARITY_BACKFILL_COOLDOWN_SECONDS = 3_600

_inflight_redline_similarity_backfill_ids: set[str] = set()


def delete_chunks_for_policy_ids(db: Session, policy_ids: list[str] | set[str]) -> int:
    """Delete document_chunks rows for the given policy IDs. Returns deleted count."""
    ids = [policy_id for policy_id in policy_ids if policy_id]
    if not ids:
        return 0
    deleted = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.policy_id.in_(ids))
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def delete_chunks_for_regulation_ids(db: Session, regulation_ids: list[str] | set[str]) -> int:
    """Delete chunks owned by government regulation rows. Returns deleted count."""
    ids = [regulation_id for regulation_id in regulation_ids if regulation_id]
    if not ids:
        return 0
    deleted = (
        db.query(DocumentChunk)
        .filter(
            or_(
                DocumentChunk.regulation_id.in_(ids),
                DocumentChunk.policy_id.in_(ids),
            )
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


def regulation_chunk_owner_filter(regulation_id: str):
    """Match chunks linked to a regulation, including pre-split policy_id rows."""
    return or_(
        DocumentChunk.regulation_id == regulation_id,
        (DocumentChunk.policy_id == regulation_id) & DocumentChunk.regulation_id.is_(None),
    )


def regulation_chunk_ids_filter(regulation_ids: list[str]):
    return or_(
        DocumentChunk.regulation_id.in_(regulation_ids),
        (
            DocumentChunk.policy_id.in_(regulation_ids)
            & DocumentChunk.regulation_id.is_(None)
        ),
    )


def purge_superseded_policy_chunks(
    db: Session,
    *,
    org_id: str | None = None,
) -> int:
    """Remove chunks for SUPERSEDED policies and regulations (RAG uses ACTIVE rows)."""
    query = (
        db.query(Policy.id)
        .filter(Policy.status == 'SUPERSEDED')
    )
    if org_id:
        query = query.filter(Policy.org_id == org_id)
    policy_ids = [row[0] for row in query.all()]
    deleted = delete_chunks_for_policy_ids(db, policy_ids)

    regulation_query = db.query(Regulation.id).filter(Regulation.status == 'SUPERSEDED')
    if org_id:
        regulation_query = regulation_query.filter(Regulation.org_id == org_id)
    regulation_ids = [row[0] for row in regulation_query.all()]
    deleted += delete_chunks_for_regulation_ids(db, regulation_ids)
    if deleted:
        logger.info(
            'purged_superseded_policy_chunks',
            org_id=org_id,
            policy_count=len(policy_ids),
            regulation_count=len(regulation_ids),
            deleted_chunks=deleted,
        )
    return deleted
_redline_similarity_backfill_lock = asyncio.Lock()
MAX_SCHEDULED_REDLINE_SIMILARITY_BACKFILLS = 2


@dataclass(frozen=True)
class RedlineInternalDocumentSimilarity:
    policy_id: str
    policy_name: str
    document_type: str
    cosine_similarity: float
    internal_chunk_count: int
    redline_chunk_count: int

    def as_metadata_dict(self) -> dict[str, Any]:
        return {
            'policy_id': self.policy_id,
            'policy_name': self.policy_name,
            'document_type': self.document_type,
            'cosine_similarity': round(self.cosine_similarity, 6),
            'internal_chunk_count': self.internal_chunk_count,
            'redline_chunk_count': self.redline_chunk_count,
        }


@dataclass(frozen=True)
class RegulatorDocumentSimilarity:
    regulator_source: str
    policy_id: str
    policy_name: str
    document_type: str
    cosine_similarity: float
    internal_chunk_count: int
    reference_chunk_count: int

    def as_metadata_dict(self) -> dict[str, Any]:
        return {
            'regulator_source': self.regulator_source,
            'policy_id': self.policy_id,
            'policy_name': self.policy_name,
            'document_type': self.document_type,
            'cosine_similarity': round(self.cosine_similarity, 6),
            'internal_chunk_count': self.internal_chunk_count,
            'reference_chunk_count': self.reference_chunk_count,
        }


def redline_storage_chunk_index(gap_analysis_id: str, chunk_index: int) -> int:
    """Assign redline chunks a dedicated index range so they never collide with internal policy chunks."""
    gap_bucket = UUID(gap_analysis_id).int % 100_000
    return REDLINE_CHUNK_INDEX_BASE + gap_bucket * REDLINE_CHUNK_INDEX_STRIDE + chunk_index


def should_retry_redline_similarity_backfill(metadata: dict[str, Any] | None) -> bool:
    """Skip repeated backfill attempts that already failed recently."""
    metadata = metadata or {}
    attempts = int(metadata.get('redline_similarity_backfill_attempts') or 0)
    if attempts >= MAX_REDLINE_SIMILARITY_BACKFILL_ATTEMPTS:
        return False

    failed_at = metadata.get('redline_similarity_backfill_failed_at')
    if not failed_at:
        return True

    try:
        failed_time = to_utc(datetime.fromisoformat(str(failed_at)))
    except (TypeError, ValueError):
        return True

    elapsed = (utc_now() - failed_time).total_seconds()
    return elapsed >= REDLINE_SIMILARITY_BACKFILL_COOLDOWN_SECONDS


def _record_redline_similarity_backfill_failure(
    db: Session,
    gap_analysis: GapAnalysis,
    *,
    metadata: dict[str, Any],
) -> None:
    updated = dict(metadata)
    updated['redline_similarity_backfill_failed_at'] = utc_now().isoformat()
    updated['redline_similarity_backfill_attempts'] = (
        int(updated.get('redline_similarity_backfill_attempts') or 0) + 1
    )
    gap_analysis.retrieval_metadata = updated
    flag_modified(gap_analysis, 'retrieval_metadata')


def _persist_redline_similarity_metadata(
    db: Session,
    gap_analysis: GapAnalysis,
    metadata: dict[str, Any],
    *,
    internal_similarity: list[RedlineInternalDocumentSimilarity] | None,
    regulator_similarity: list[RegulatorDocumentSimilarity] | None,
) -> None:
    updated = apply_redline_similarity_metadata(
        metadata,
        internal_similarity=internal_similarity,
        regulator_similarity=regulator_similarity,
    )
    updated.pop('redline_similarity_backfill_failed_at', None)
    updated['redline_similarity_backfill_attempts'] = 0
    gap_analysis.retrieval_metadata = updated
    flag_modified(gap_analysis, 'retrieval_metadata')


def chunk_policy_name_value(
    *,
    policy_name: str = '',
    policy_type: str | None = None,
    regulator_source: str | None = None,
) -> str:
    if regulator_source:
        return regulator_source.strip().upper()
    return normalize_chunk_policy_name(policy_name, policy_type)


def load_embedded_vectors(
    db: Session,
    *,
    org_id: str,
    policy_id: str | None = None,
    regulation_id: str | None = None,
    document_type: str | None = None,
    policy_name: str | None = None,
) -> list[list[float]]:
    query = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.org_id == org_id,
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(DocumentChunk.chunk_index.asc())
    )
    if regulation_id is not None:
        query = query.filter(regulation_chunk_owner_filter(regulation_id))
    elif policy_id is not None:
        query = query.filter(DocumentChunk.policy_id == policy_id)
    else:
        return []
    if document_type is not None:
        query = query.filter(DocumentChunk.document_type == document_type)
    if policy_name is not None:
        query = query.filter(DocumentChunk.policy_name == policy_name)
    rows = query.all()
    vectors: list[list[float]] = []
    for row in rows:
        if row.embedding is None:
            continue
        vectors.append([float(value) for value in row.embedding])
    return vectors


def load_internal_policy_vectors(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
) -> list[list[float]]:
    """Load stored OpenAI embeddings for an internal policy document."""
    scoped_policy_name = chunk_policy_name_value(
        policy_name=policy_name,
        policy_type=INTERNAL_POLICY_TYPE,
    )
    vectors = load_embedded_vectors(
        db,
        org_id=org_id,
        policy_id=policy_id,
        document_type=INTERNAL_POLICY_TYPE,
        policy_name=scoped_policy_name,
    )
    if vectors:
        return vectors
    # Legacy uploads may have stored chunks under a FINRA/SEC/CFTC label.
    legacy_regulator = resolve_internal_regulator_source(policy_name)
    if legacy_regulator and legacy_regulator != scoped_policy_name:
        vectors = load_embedded_vectors(
            db,
            org_id=org_id,
            policy_id=policy_id,
            document_type=INTERNAL_POLICY_TYPE,
            policy_name=legacy_regulator,
        )
        if vectors:
            return vectors
    return load_embedded_vectors(
        db,
        org_id=org_id,
        policy_id=policy_id,
        document_type=INTERNAL_POLICY_TYPE,
    )


def load_redline_chunk_embeddings(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    gap_analysis_id: str,
) -> list[list[float]]:
    """Load stored OpenAI embeddings for one generated redline revision."""
    rows = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.org_id == org_id,
            DocumentChunk.policy_id == policy_id,
            DocumentChunk.document_type == REDLINE_CHUNK_TYPE,
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    vectors: list[list[float]] = []
    for row in rows:
        metadata = row.metadata_json or {}
        if metadata.get('gap_analysis_id') != gap_analysis_id:
            continue
        if row.embedding is None:
            continue
        vectors.append([float(value) for value in row.embedding])
    return vectors


def replace_redline_chunk_embeddings(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    gap_analysis_id: str,
    policy_name: str,
    chunks: list[TextChunk],
    embeddings: list[list[float]],
    embedding_model: str,
) -> None:
    """Persist redline chunk embeddings so later comparisons reuse the same vectors."""
    target_indexes = {
        redline_storage_chunk_index(gap_analysis_id, chunk.index)
        for chunk in chunks
    }
    existing = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.org_id == org_id,
            DocumentChunk.policy_id == policy_id,
            DocumentChunk.document_type == REDLINE_CHUNK_TYPE,
        )
        .all()
    )
    for row in existing:
        metadata = row.metadata_json or {}
        # Remove this gap's prior vectors and any hash-colliding indexes from other gaps.
        if (
            metadata.get('gap_analysis_id') == gap_analysis_id
            or row.chunk_index in target_indexes
        ):
            db.delete(row)
    db.flush()

    scoped_policy_name = chunk_policy_name_value(
        policy_name=policy_name,
        policy_type=INTERNAL_POLICY_TYPE,
    )
    for chunk, embedding in zip(chunks, embeddings, strict=False):
        if embedding is None:
            continue
        db.add(
            DocumentChunk(
                id=str(uuid4()),
                org_id=org_id,
                policy_id=policy_id,
                chunk_index=redline_storage_chunk_index(gap_analysis_id, chunk.index),
                content=chunk.content,
                document_type=REDLINE_CHUNK_TYPE,
                policy_name=scoped_policy_name,
                embedding=embedding,
                metadata_json={
                    **chunk.metadata,
                    'gap_analysis_id': gap_analysis_id,
                    'policy_type': REDLINE_CHUNK_TYPE,
                    'embedding_model': embedding_model,
                    'embedded': True,
                },
            )
        )


def _resolve_reference_policy_for_regulator(
    db: Session,
    *,
    org_id: str,
    regulator_source: str,
) -> Regulation | None:
    reference_policies = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.rule_code.is_(None),
        )
        .order_by(Regulation.name.asc())
        .all()
    )
    for reference_policy in reference_policies:
        reference_regulator = resolve_internal_regulator_source(reference_policy.name) or (
            reference_policy.name.split()[0].upper()
        )
        if reference_regulator == regulator_source:
            return reference_policy
    return None


def load_regulator_reference_vectors_for_source(
    db: Session,
    *,
    org_id: str,
    regulator_source: str,
) -> tuple[Regulation | None, list[list[float]]]:
    """Return the active regulation document and its embeddings for one regulator."""
    reference_policy = _resolve_reference_policy_for_regulator(
        db,
        org_id=org_id,
        regulator_source=regulator_source,
    )
    if reference_policy is None:
        return None, []

    reference_vectors = load_embedded_vectors(
        db,
        org_id=org_id,
        regulation_id=reference_policy.id,
        document_type=REGULATORY_REFERENCE_TYPE,
        policy_name=regulator_source,
    )
    if not reference_vectors:
        reference_vectors = load_embedded_vectors(
            db,
            org_id=org_id,
            regulation_id=reference_policy.id,
            document_type=REGULATORY_REFERENCE_TYPE,
        )
    return reference_policy, reference_vectors


def load_all_regulator_reference_vectors(
    db: Session,
    *,
    org_id: str,
) -> list[list[float]]:
    """Pool stored embeddings from active FINRA, SEC, and CFTC regulation documents."""
    from app.services.policy_comparison_service import REGULATOR_SOURCES

    pooled: list[list[float]] = []
    for regulator_source in REGULATOR_SOURCES:
        _policy, vectors = load_regulator_reference_vectors_for_source(
            db,
            org_id=org_id,
            regulator_source=regulator_source,
        )
        pooled.extend(vectors)
    return pooled


def compare_internal_policy_to_regulator_documents(
    db: Session,
    *,
    org_id: str,
    internal_policy_id: str,
    internal_policy_name: str | None = None,
) -> list[RegulatorDocumentSimilarity]:
    """Compare one internal policy against FINRA, SEC, and CFTC reference documents."""
    internal_policy = (
        db.query(Policy)
        .filter(
            Policy.id == internal_policy_id,
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
        )
        .one_or_none()
    )
    if internal_policy is None:
        return []

    scoped_policy_name = chunk_policy_name_value(
        policy_name=internal_policy_name or internal_policy.name,
        policy_type=internal_policy.type,
    )
    internal_vectors = load_embedded_vectors(
        db,
        org_id=org_id,
        policy_id=internal_policy_id,
        document_type=INTERNAL_POLICY_TYPE,
        policy_name=scoped_policy_name,
    )
    if not internal_vectors:
        internal_vectors = load_embedded_vectors(
            db,
            org_id=org_id,
            policy_id=internal_policy_id,
            document_type=INTERNAL_POLICY_TYPE,
        )
    if not internal_vectors:
        return []

    from app.services.policy_comparison_service import REGULATOR_SOURCES
    from app.services.redline_vector_validation_service import ALL_REGULATOR_LABEL

    results: list[RegulatorDocumentSimilarity] = []
    pooled_vectors: list[list[float]] = []
    for regulator_source in REGULATOR_SOURCES:
        reference_policy, reference_vectors = load_regulator_reference_vectors_for_source(
            db,
            org_id=org_id,
            regulator_source=regulator_source,
        )
        if reference_policy is None or not reference_vectors:
            continue

        pooled_vectors.extend(reference_vectors)
        similarity = mean_max_cosine_similarity(internal_vectors, reference_vectors)
        results.append(
            RegulatorDocumentSimilarity(
                regulator_source=regulator_source,
                policy_id=reference_policy.id,
                policy_name=reference_policy.name,
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=similarity,
                internal_chunk_count=len(internal_vectors),
                reference_chunk_count=len(reference_vectors),
            )
        )
    results.sort(key=lambda item: item.cosine_similarity, reverse=True)
    if pooled_vectors:
        overall = mean_max_cosine_similarity(internal_vectors, pooled_vectors)
        results.insert(
            0,
            RegulatorDocumentSimilarity(
                regulator_source='ALL',
                policy_id='all-regulators',
                policy_name=ALL_REGULATOR_LABEL,
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=overall,
                internal_chunk_count=len(internal_vectors),
                reference_chunk_count=len(pooled_vectors),
            ),
        )
    return results


def compare_redline_to_internal_policy(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
    redline_vectors: list[list[float]],
) -> list[RedlineInternalDocumentSimilarity]:
    """Compare stored redline embeddings against the matching internal policy document."""
    if not redline_vectors:
        return []

    internal_vectors = load_internal_policy_vectors(
        db,
        org_id=org_id,
        policy_id=policy_id,
        policy_name=policy_name,
    )
    if not internal_vectors:
        return []

    similarity = mean_max_cosine_similarity(redline_vectors, internal_vectors)
    return [
        RedlineInternalDocumentSimilarity(
            policy_id=policy_id,
            policy_name=policy_name,
            document_type=INTERNAL_POLICY_TYPE,
            cosine_similarity=similarity,
            internal_chunk_count=len(internal_vectors),
            redline_chunk_count=len(redline_vectors),
        )
    ]


def compare_redline_to_regulator_documents(
    db: Session,
    *,
    org_id: str,
    policy_name: str,
    redline_vectors: list[list[float]],
) -> list[RegulatorDocumentSimilarity]:
    """Compare generated redline embeddings against FINRA, SEC, and CFTC reference documents."""
    del policy_name  # Free-text internal names are not regulator-scoped.
    if not redline_vectors:
        return []

    from app.services.policy_comparison_service import REGULATOR_SOURCES
    from app.services.redline_vector_validation_service import ALL_REGULATOR_LABEL

    results: list[RegulatorDocumentSimilarity] = []
    pooled_vectors: list[list[float]] = []
    for regulator_source in REGULATOR_SOURCES:
        reference_policy, reference_vectors = load_regulator_reference_vectors_for_source(
            db,
            org_id=org_id,
            regulator_source=regulator_source,
        )
        if reference_policy is None or not reference_vectors:
            continue

        pooled_vectors.extend(reference_vectors)
        similarity = mean_max_cosine_similarity(redline_vectors, reference_vectors)
        results.append(
            RegulatorDocumentSimilarity(
                regulator_source=regulator_source,
                policy_id=reference_policy.id,
                policy_name=reference_policy.name,
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=similarity,
                internal_chunk_count=len(redline_vectors),
                reference_chunk_count=len(reference_vectors),
            )
        )

    results.sort(key=lambda item: item.cosine_similarity, reverse=True)
    if pooled_vectors:
        overall = mean_max_cosine_similarity(redline_vectors, pooled_vectors)
        results.insert(
            0,
            RegulatorDocumentSimilarity(
                regulator_source='ALL',
                policy_id='all-regulators',
                policy_name=ALL_REGULATOR_LABEL,
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=overall,
                internal_chunk_count=len(redline_vectors),
                reference_chunk_count=len(pooled_vectors),
            ),
        )
    return results


def compare_redline_to_regulator_documents_with_stored_vectors(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
    gap_analysis_id: str,
    redline_vectors: list[list[float]] | None = None,
) -> list[RegulatorDocumentSimilarity]:
    """Compare stored redline and government embeddings for one gap analysis."""
    vectors = list(redline_vectors or [])
    if not vectors:
        vectors = load_redline_chunk_embeddings(
            db,
            org_id=org_id,
            policy_id=policy_id,
            gap_analysis_id=gap_analysis_id,
        )
    if not vectors:
        return []

    return compare_redline_to_regulator_documents(
        db,
        org_id=org_id,
        policy_name=policy_name,
        redline_vectors=vectors,
    )


def apply_redline_similarity_metadata(
    retrieval_metadata: dict[str, Any] | None,
    *,
    internal_similarity: list[RedlineInternalDocumentSimilarity] | None = None,
    regulator_similarity: list[RegulatorDocumentSimilarity] | None = None,
) -> dict[str, Any]:
    metadata = dict(retrieval_metadata or {})
    if internal_similarity:
        metadata['redline_internal_document_similarity'] = [
            item.as_metadata_dict() for item in internal_similarity
        ]
    if regulator_similarity:
        metadata['redline_regulator_document_similarity'] = [
            item.as_metadata_dict() for item in regulator_similarity
        ]
    return metadata


def redline_similarity_metadata_complete(metadata: dict[str, Any] | None) -> bool:
    if not metadata:
        return False
    internal_items = metadata.get('redline_internal_document_similarity') or []
    regulator_items = metadata.get('redline_regulator_document_similarity') or []
    return bool(internal_items and regulator_items)


async def backfill_redline_similarity_for_revision(
    revision_id: str,
    gap_analysis_id: str,
    org_id: str,
) -> None:
    """Background worker that computes redline similarity without holding request DB connections."""
    async with _redline_similarity_backfill_lock:
        if revision_id in _inflight_redline_similarity_backfill_ids:
            logger.info(
                'redline_similarity_backfill_skipped_inflight',
                revision_id=revision_id,
            )
            return
        _inflight_redline_similarity_backfill_ids.add(revision_id)

    redline_chunks: list[TextChunk] = []
    redline_vectors: list[list[float]] = []
    policy_id = ''
    policy_name = ''
    redline_file_url = ''

    try:
        async with background_db_slot():
            settings = get_settings()
            if optional_embedding_provider(org_id) is None:
                return

            with session_scope(commit=False) as db:
                record = (
                    db.query(PolicyRevision, GapAnalysis, Policy)
                    .join(GapAnalysis, GapAnalysis.id == PolicyRevision.gap_analysis_id)
                    .join(Policy, Policy.id == PolicyRevision.policy_id)
                    .filter(
                        PolicyRevision.id == revision_id,
                        PolicyRevision.gap_analysis_id == gap_analysis_id,
                        PolicyRevision.org_id == org_id,
                        GapAnalysis.org_id == org_id,
                        Policy.org_id == org_id,
                    )
                    .one_or_none()
                )
                if record is None:
                    return
                revision, gap_analysis, policy = record
                metadata = dict(gap_analysis.retrieval_metadata or {})
                if redline_similarity_metadata_complete(metadata):
                    return
                if not should_retry_redline_similarity_backfill(metadata):
                    logger.info(
                        'redline_similarity_backfill_skipped_recent_failure',
                        revision_id=revision_id,
                        gap_analysis_id=gap_analysis_id,
                    )
                    return
                if not revision.redline_file_url:
                    return

                redline_file_url = revision.redline_file_url
                policy_name = policy.name
                policy_id = policy.id
                redline_vectors = load_redline_chunk_embeddings(
                    db,
                    org_id=org_id,
                    policy_id=policy_id,
                    gap_analysis_id=gap_analysis_id,
                )

            if not redline_vectors:
                from app.services.document_ingestion import chunk_publication_text, generate_embeddings
                from app.services.redline_document import extract_policy_paragraphs, policy_text_from_paragraphs
                from app.services.storage_service import download_file

                try:
                    redline_bytes = download_file(redline_file_url)
                except Exception:
                    logger.warning(
                        'redline_similarity_backfill_download_failed',
                        revision_id=revision_id,
                        gap_analysis_id=gap_analysis_id,
                    )
                    with session_scope() as db:
                        gap_analysis = (
                            db.query(GapAnalysis)
                            .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == org_id)
                            .one_or_none()
                        )
                        if gap_analysis is not None:
                            _record_redline_similarity_backfill_failure(
                                db,
                                gap_analysis,
                                metadata=dict(gap_analysis.retrieval_metadata or {}),
                            )
                    return

                from app.services.tracked_redline_document import extract_accepted_redline_text

                redline_text = extract_accepted_redline_text(redline_bytes).strip()
                if not redline_text:
                    redline_text = policy_text_from_paragraphs(
                        extract_policy_paragraphs(redline_bytes)
                    ).strip()
                if not redline_text:
                    return

                redline_chunks = chunk_publication_text(f'{policy_name} redline', redline_text)
                redline_vectors = await generate_embeddings(redline_chunks, settings, org_id=org_id)
                if not redline_vectors:
                    return

            with session_scope() as db:
                gap_analysis = (
                    db.query(GapAnalysis)
                    .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == org_id)
                    .one_or_none()
                )
                policy = (
                    db.query(Policy)
                    .filter(Policy.id == policy_id, Policy.org_id == org_id)
                    .one_or_none()
                )
                if gap_analysis is None or policy is None:
                    return

                metadata = dict(gap_analysis.retrieval_metadata or {})
                if redline_similarity_metadata_complete(metadata):
                    return

                if redline_chunks and redline_vectors:
                    replace_redline_chunk_embeddings(
                        db,
                        org_id=org_id,
                        policy_id=policy.id,
                        gap_analysis_id=gap_analysis.id,
                        policy_name=policy.name,
                        chunks=redline_chunks,
                        embeddings=redline_vectors,
                        embedding_model=embedding_model_label(org_id, settings),
                    )

                internal_similarity = compare_redline_to_internal_policy(
                    db,
                    org_id=org_id,
                    policy_id=policy.id,
                    policy_name=policy.name,
                    redline_vectors=redline_vectors,
                )
                regulator_similarity = compare_redline_to_regulator_documents_with_stored_vectors(
                    db,
                    org_id=org_id,
                    policy_id=policy.id,
                    policy_name=policy.name,
                    gap_analysis_id=gap_analysis.id,
                    redline_vectors=redline_vectors,
                )
                _persist_redline_similarity_metadata(
                    db,
                    gap_analysis,
                    metadata,
                    internal_similarity=internal_similarity or None,
                    regulator_similarity=regulator_similarity or None,
                )
    except Exception as exc:
        logger.exception(
            'redline_similarity_backfill_failed',
            revision_id=revision_id,
            gap_analysis_id=gap_analysis_id,
            error_type=type(exc).__name__,
        )
        try:
            with session_scope() as db:
                gap_analysis = (
                    db.query(GapAnalysis)
                    .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == org_id)
                    .one_or_none()
                )
                if gap_analysis is not None:
                    _record_redline_similarity_backfill_failure(
                        db,
                        gap_analysis,
                        metadata=dict(gap_analysis.retrieval_metadata or {}),
                    )
        except Exception:
            logger.exception(
                'redline_similarity_backfill_failure_marker_failed',
                revision_id=revision_id,
                gap_analysis_id=gap_analysis_id,
            )
    finally:
        async with _redline_similarity_backfill_lock:
            _inflight_redline_similarity_backfill_ids.discard(revision_id)


def schedule_redline_similarity_backfills(
    background_tasks: Any,
    org_id: str,
    pending_revisions: list[dict[str, str | None]],
) -> None:
    """Queue background similarity backfill for pending revisions missing vector scores."""
    add_task = getattr(background_tasks, 'add_task', None)
    if add_task is None:
        return

    scheduled = 0
    for item in pending_revisions:
        if scheduled >= MAX_SCHEDULED_REDLINE_SIMILARITY_BACKFILLS:
            break
        if not item.get('redline_url'):
            continue
        revision_id = str(item.get('revision_id') or '')
        gap_analysis_id = str(item.get('gap_analysis_id') or '')
        if not revision_id or not gap_analysis_id:
            continue
        if revision_id in _inflight_redline_similarity_backfill_ids:
            continue
        add_task(
            backfill_redline_similarity_for_revision,
            revision_id,
            gap_analysis_id,
            org_id,
        )
        scheduled += 1


def redline_internal_similarity_fields(
    metadata: dict[str, Any] | None,
) -> dict[str, float | str | None]:
    """Extract redline-vs-internal vector comparison metadata."""
    if not metadata:
        return {
            'redline_internal_cosine_similarity': None,
            'redline_internal_policy_name': None,
        }
    items = metadata.get('redline_internal_document_similarity') or []
    if not isinstance(items, list) or not items:
        return {
            'redline_internal_cosine_similarity': None,
            'redline_internal_policy_name': None,
        }
    primary = items[0]
    if not isinstance(primary, dict):
        return {
            'redline_internal_cosine_similarity': None,
            'redline_internal_policy_name': None,
        }
    similarity = primary.get('cosine_similarity')
    return {
        'redline_internal_cosine_similarity': (
            float(similarity) if similarity is not None else None
        ),
        'redline_internal_policy_name': (
            str(primary.get('policy_name') or '').strip() or None
        ),
    }


def redline_regulator_similarity_fields(
    metadata: dict[str, Any] | None,
) -> dict[str, float | str | None]:
    """Extract redline-vs-government vector comparison metadata.

    Prefers the pooled ALL-regulators score when present; otherwise the strongest
    individual FINRA/SEC/CFTC match.
    """
    if not metadata:
        return {
            'redline_regulator_cosine_similarity': None,
            'redline_regulator_reference_policy_name': None,
        }
    items = metadata.get('redline_regulator_document_similarity') or []
    if not isinstance(items, list) or not items:
        return {
            'redline_regulator_cosine_similarity': None,
            'redline_regulator_reference_policy_name': None,
        }

    dict_items = [item for item in items if isinstance(item, dict)]
    if not dict_items:
        return {
            'redline_regulator_cosine_similarity': None,
            'redline_regulator_reference_policy_name': None,
        }

    overall = next(
        (
            item
            for item in dict_items
            if str(item.get('regulator_source') or '').upper() == 'ALL'
        ),
        None,
    )
    primary = overall or max(
        dict_items,
        key=lambda item: float(item.get('cosine_similarity') or 0.0),
    )
    similarity = primary.get('cosine_similarity')
    return {
        'redline_regulator_cosine_similarity': (
            float(similarity) if similarity is not None else None
        ),
        'redline_regulator_reference_policy_name': (
            str(primary.get('policy_name') or '').strip() or None
        ),
    }


async def compare_internal_policy_to_regulator_documents_with_fallback(
    db: Session | None,
    *,
    org_id: str,
    internal_policy_id: str,
    internal_policy_name: str | None,
    internal_policy_text: str,
    settings: Settings,
) -> list[RegulatorDocumentSimilarity]:
    """Compare stored vectors, embedding on the fly when chunk rows are missing."""

    def read_stored_results(scoped_db: Session) -> list[RegulatorDocumentSimilarity]:
        return compare_internal_policy_to_regulator_documents(
            scoped_db,
            org_id=org_id,
            internal_policy_id=internal_policy_id,
            internal_policy_name=internal_policy_name,
        )

    if db is not None:
        results = read_stored_results(db)
    else:
        with session_scope(commit=False, org_id=org_id) as scoped_db:
            results = read_stored_results(scoped_db)
    if results:
        return results

    from app.services.document_ingestion import chunk_publication_text, generate_embeddings

    if not internal_policy_text.strip() or optional_embedding_provider(org_id) is None:
        return []

    from app.services.policy_comparison_service import REGULATOR_SOURCES

    with session_scope(commit=False, org_id=org_id) as scoped_db:
        internal_policy = (
            scoped_db.query(Policy)
            .filter(Policy.id == internal_policy_id, Policy.org_id == org_id)
            .one_or_none()
        )
        if internal_policy is None:
            return []
        scoped_policy_name = chunk_policy_name_value(
            policy_name=internal_policy_name or internal_policy.name,
            policy_type=internal_policy.type,
        )

    internal_chunks = chunk_publication_text(scoped_policy_name, internal_policy_text)
    internal_vectors = await generate_embeddings(internal_chunks, settings, org_id=org_id)
    if not internal_vectors:
        return []

    results: list[RegulatorDocumentSimilarity] = []
    with session_scope(commit=False, org_id=org_id) as scoped_db:
        for regulator_source in REGULATOR_SOURCES:
            reference_policy = _resolve_reference_policy_for_regulator(
                scoped_db,
                org_id=org_id,
                regulator_source=regulator_source,
            )
            if reference_policy is None:
                continue
            reference_policy_id = reference_policy.id
            reference_policy_name = reference_policy.name
            reference_vectors = load_embedded_vectors(
                scoped_db,
                org_id=org_id,
                regulation_id=reference_policy_id,
                document_type=REGULATORY_REFERENCE_TYPE,
                policy_name=regulator_source,
            )
            reference_text = ''
            if not reference_vectors:
                reference_text = '\n\n'.join(
                    chunk.content
                    for chunk in scoped_db.query(DocumentChunk)
                    .filter(
                        regulation_chunk_owner_filter(reference_policy_id),
                        DocumentChunk.policy_name == regulator_source,
                    )
                    .order_by(DocumentChunk.chunk_index.asc())
                    .all()
                )
                if not reference_text.strip():
                    reference_text = '\n\n'.join(
                        chunk.content
                        for chunk in scoped_db.query(DocumentChunk)
                        .filter(regulation_chunk_owner_filter(reference_policy_id))
                        .order_by(DocumentChunk.chunk_index.asc())
                        .all()
                    )
            if not reference_vectors:
                if not reference_text.strip():
                    continue
                reference_chunks = chunk_publication_text(regulator_source, reference_text)
                reference_vectors = await generate_embeddings(reference_chunks, settings, org_id=org_id)
            if not reference_vectors:
                continue
            similarity = mean_max_cosine_similarity(internal_vectors, reference_vectors)
            results.append(
                RegulatorDocumentSimilarity(
                    regulator_source=regulator_source,
                    policy_id=reference_policy_id,
                    policy_name=reference_policy_name,
                    document_type=REGULATORY_REFERENCE_TYPE,
                    cosine_similarity=similarity,
                    internal_chunk_count=len(internal_vectors),
                    reference_chunk_count=len(reference_vectors),
                )
            )
    return results


def chunk_document_type_value(
    *,
    policy_type: str | None = None,
    publication: bool = False,
    context_document: bool = False,
) -> str:
    if publication:
        from app.services.policy_types import REGULATORY_PUBLICATION_CHUNK_TYPE

        return REGULATORY_PUBLICATION_CHUNK_TYPE
    if context_document:
        from app.services.policy_types import CONTEXT_DOCUMENT_CHUNK_TYPE

        return CONTEXT_DOCUMENT_CHUNK_TYPE
    return chunk_document_type_for_policy(policy_type)
