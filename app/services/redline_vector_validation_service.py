"""Validate generated redline DOCX files against government regulation embeddings."""

from __future__ import annotations

import math
from dataclasses import dataclass

import structlog
from sqlalchemy.orm import Session

from app.ai import optional_embedding_provider
from app.core.config import Settings
from app.models.core import DocumentChunk
from app.services.document_ingestion import TextChunk, chunk_publication_text, generate_embeddings
from app.services.redline_document import extract_policy_paragraphs, policy_text_from_paragraphs

logger = structlog.get_logger(__name__)

NO_RELATED_REDLINE_CONTENT_MESSAGE = (
    'No related content was found in the generated redline after multiple attempts.'
)
ALL_REGULATOR_LABEL = 'FINRA, SEC, and CFTC Regulations'


@dataclass(frozen=True)
class RedlineVectorValidationResult:
    accepted: bool
    similarity: float
    threshold: float
    skipped: bool = False
    reason: str | None = None
    redline_vectors: list[list[float]] | None = None
    redline_chunks: list[TextChunk] | None = None


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (left_norm * right_norm)))


def mean_max_cosine_similarity(
    query_vectors: list[list[float]],
    reference_vectors: list[list[float]],
) -> float:
    """Average, over each query vector, of its best match against any reference vector."""
    if not query_vectors or not reference_vectors:
        return 0.0
    scores = [
        max(cosine_similarity(query, reference) for reference in reference_vectors)
        for query in query_vectors
    ]
    return sum(scores) / len(scores)


def load_policy_chunk_embeddings(
    db: Session,
    *,
    policy_id: str,
    org_id: str,
) -> list[list[float]]:
    rows = (
        db.query(DocumentChunk)
        .filter(
            DocumentChunk.policy_id == policy_id,
            DocumentChunk.org_id == org_id,
            DocumentChunk.embedding.isnot(None),
        )
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    vectors: list[list[float]] = []
    for row in rows:
        if row.embedding is None:
            continue
        vectors.append([float(value) for value in row.embedding])
    return vectors


def load_all_regulator_reference_embeddings(
    db: Session,
    *,
    org_id: str,
) -> list[list[float]]:
    """Load stored embeddings for active FINRA, SEC, and CFTC regulation documents."""
    from app.services.document_chunk_service import load_all_regulator_reference_vectors

    return load_all_regulator_reference_vectors(db, org_id=org_id)


async def _embed_redline_document(
    *,
    org_id: str,
    policy_name: str,
    redline_bytes: bytes,
    fallback_text: str,
    settings: Settings,
) -> tuple[list[TextChunk], list[list[float]], str | None]:
    from app.services.tracked_redline_document import extract_accepted_redline_text

    # Use accepted/proposed text so tracked-change markup does not drop edited lines.
    redline_text = extract_accepted_redline_text(redline_bytes).strip()
    if not redline_text:
        redline_text = policy_text_from_paragraphs(extract_policy_paragraphs(redline_bytes)).strip()
    if not redline_text:
        redline_text = fallback_text.strip()
    if not redline_text:
        return [], [], 'empty_redline_text'

    redline_chunks = chunk_publication_text(f'{policy_name} redline', redline_text)
    redline_vectors = await generate_embeddings(redline_chunks, settings, org_id=org_id)
    if not redline_vectors:
        if optional_embedding_provider(org_id) is not None:
            return redline_chunks, [], 'redline_embedding_failed'
        return redline_chunks, [], 'openai_api_key_missing'
    return redline_chunks, redline_vectors, None


async def validate_redline_against_internal_policy(
    db: Session | None,
    *,
    policy_id: str,
    org_id: str,
    policy_name: str,
    redline_bytes: bytes,
    internal_policy_text: str,
    settings: Settings,
    policy_vectors: list[list[float]] | None = None,
) -> RedlineVectorValidationResult:
    """Legacy helper: compare redline embeddings to the internal policy document."""
    threshold = settings.redline_min_vector_similarity
    redline_chunks, redline_vectors, embed_error = await _embed_redline_document(
        org_id=org_id,
        policy_name=policy_name,
        redline_bytes=redline_bytes,
        fallback_text=internal_policy_text,
        settings=settings,
    )
    if embed_error == 'empty_redline_text':
        return RedlineVectorValidationResult(
            accepted=False,
            similarity=0.0,
            threshold=threshold,
            reason='empty_redline_text',
        )
    if embed_error == 'redline_embedding_failed':
        # OpenAI quota/network failures will not improve by regenerating the draft.
        # Skip the gate outside production so local review flows stay unblocked.
        if not settings.is_production:
            logger.warning(
                'redline_vector_validation_skipped',
                policy_id=policy_id,
                org_id=org_id,
                reason='redline_embedding_failed',
            )
            return RedlineVectorValidationResult(
                accepted=True,
                similarity=1.0,
                threshold=threshold,
                skipped=True,
                reason='redline_embedding_failed',
                redline_chunks=redline_chunks,
            )
        return RedlineVectorValidationResult(
            accepted=False,
            similarity=0.0,
            threshold=threshold,
            reason='redline_embedding_failed',
            redline_chunks=redline_chunks,
        )
    if embed_error == 'openai_api_key_missing':
        logger.warning(
            'redline_vector_validation_skipped',
            policy_id=policy_id,
            org_id=org_id,
            reason='openai_api_key_missing',
        )
        return RedlineVectorValidationResult(
            accepted=True,
            similarity=1.0,
            threshold=threshold,
            skipped=True,
            reason='openai_api_key_missing',
            redline_chunks=redline_chunks,
        )

    vectors = list(policy_vectors or [])
    if not vectors and db is not None:
        vectors = load_policy_chunk_embeddings(db, policy_id=policy_id, org_id=org_id)

    if not vectors:
        if settings.is_production:
            return RedlineVectorValidationResult(
                accepted=False,
                similarity=0.0,
                threshold=threshold,
                reason='policy_embedding_unavailable',
                redline_vectors=redline_vectors,
                redline_chunks=redline_chunks,
            )
        logger.warning(
            'redline_vector_validation_skipped',
            policy_id=policy_id,
            org_id=org_id,
            reason='policy_embedding_unavailable',
        )
        return RedlineVectorValidationResult(
            accepted=True,
            similarity=1.0,
            threshold=threshold,
            skipped=True,
            reason='policy_embedding_unavailable',
            redline_vectors=redline_vectors,
            redline_chunks=redline_chunks,
        )

    similarity = mean_max_cosine_similarity(redline_vectors, vectors)
    accepted = similarity >= threshold
    logger.info(
        'redline_internal_vector_validation_completed',
        policy_id=policy_id,
        org_id=org_id,
        similarity=round(similarity, 6),
        threshold=threshold,
        accepted=accepted,
        policy_vector_count=len(vectors),
        redline_vector_count=len(redline_vectors),
    )
    return RedlineVectorValidationResult(
        accepted=accepted,
        similarity=similarity,
        threshold=threshold,
        redline_vectors=redline_vectors,
        redline_chunks=redline_chunks,
    )


async def validate_redline_against_regulator_documents(
    db: Session | None,
    *,
    org_id: str,
    policy_name: str,
    redline_bytes: bytes,
    fallback_text: str,
    settings: Settings,
    regulator_vectors: list[list[float]] | None = None,
) -> RedlineVectorValidationResult:
    """Accept redlines whose embeddings are close enough to FINRA/SEC/CFTC regulations.

    Embeddings from all three Latest Regulations documents are pooled, then compared to
    the newly generated redline with mean-max cosine similarity. Scores below
    ``REDLINE_MIN_VECTOR_SIMILARITY`` (default 0.25) are rejected so drafting can retry.
    """
    threshold = settings.redline_min_vector_similarity
    redline_chunks, redline_vectors, embed_error = await _embed_redline_document(
        org_id=org_id,
        policy_name=policy_name,
        redline_bytes=redline_bytes,
        fallback_text=fallback_text,
        settings=settings,
    )
    if embed_error == 'empty_redline_text':
        return RedlineVectorValidationResult(
            accepted=False,
            similarity=0.0,
            threshold=threshold,
            reason='empty_redline_text',
        )
    if embed_error == 'redline_embedding_failed':
        # OpenAI quota/network failures will not improve by regenerating the draft.
        # Skip the gate outside production so local review flows stay unblocked.
        if not settings.is_production:
            logger.warning(
                'redline_regulator_vector_validation_skipped',
                org_id=org_id,
                reason='redline_embedding_failed',
            )
            return RedlineVectorValidationResult(
                accepted=True,
                similarity=1.0,
                threshold=threshold,
                skipped=True,
                reason='redline_embedding_failed',
                redline_chunks=redline_chunks,
            )
        return RedlineVectorValidationResult(
            accepted=False,
            similarity=0.0,
            threshold=threshold,
            reason='redline_embedding_failed',
            redline_chunks=redline_chunks,
        )
    if embed_error == 'openai_api_key_missing':
        logger.warning(
            'redline_regulator_vector_validation_skipped',
            org_id=org_id,
            reason='openai_api_key_missing',
        )
        return RedlineVectorValidationResult(
            accepted=True,
            similarity=1.0,
            threshold=threshold,
            skipped=True,
            reason='openai_api_key_missing',
            redline_chunks=redline_chunks,
        )

    vectors = list(regulator_vectors or [])
    if not vectors and db is not None:
        vectors = load_all_regulator_reference_embeddings(db, org_id=org_id)

    if not vectors:
        if settings.is_production:
            return RedlineVectorValidationResult(
                accepted=False,
                similarity=0.0,
                threshold=threshold,
                reason='regulator_embedding_unavailable',
                redline_vectors=redline_vectors,
                redline_chunks=redline_chunks,
            )
        logger.warning(
            'redline_regulator_vector_validation_skipped',
            org_id=org_id,
            reason='regulator_embedding_unavailable',
        )
        return RedlineVectorValidationResult(
            accepted=True,
            similarity=1.0,
            threshold=threshold,
            skipped=True,
            reason='regulator_embedding_unavailable',
            redline_vectors=redline_vectors,
            redline_chunks=redline_chunks,
        )

    similarity = mean_max_cosine_similarity(redline_vectors, vectors)
    accepted = similarity >= threshold
    logger.info(
        'redline_regulator_vector_validation_completed',
        org_id=org_id,
        similarity=round(similarity, 6),
        threshold=threshold,
        accepted=accepted,
        regulator_vector_count=len(vectors),
        redline_vector_count=len(redline_vectors),
        reference_label=ALL_REGULATOR_LABEL,
    )
    return RedlineVectorValidationResult(
        accepted=accepted,
        similarity=similarity,
        threshold=threshold,
        redline_vectors=redline_vectors,
        redline_chunks=redline_chunks,
    )
