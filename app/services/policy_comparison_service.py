"""Vector retrieval comparing internal firm policies to government reference documents."""

from __future__ import annotations

import asyncio
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.ai import optional_embedding_provider
from app.core.config import Settings
from app.db.session import SessionLocal, bind_data_plane_session
from app.models.core import DocumentChunk, Regulation
from app.services.document_chunk_service import COSINE_RELATEDNESS_FLOOR
from app.services.policy_types import (
    REGULATORY_REFERENCE_TYPE,
    resolve_internal_regulator_source,
)

logger = structlog.get_logger(__name__)

MAX_GOVERNMENT_EXCERPTS = 6
MAX_INTERNAL_POLICY_GOVERNMENT_EXCERPTS = 4
MAX_INTERNAL_POLICY_ALL_REGULATORS_EXCERPTS = 9
MAX_BUSINESS_CHANGE_GOVERNMENT_EXCERPTS = 9
MAX_GOVERNMENT_EXCERPT_CHARS = 4_000
MAX_REGULATORY_REFERENCE_ATTACHMENT_CHARS = 48_000
REGULATOR_SOURCES = ('SEC', 'FINRA', 'CFTC')


def _regulation_chunk_filter(regulation_id: str):
    return or_(
        DocumentChunk.regulation_id == regulation_id,
        and_(DocumentChunk.policy_id == regulation_id, DocumentChunk.regulation_id.is_(None)),
    )


def _regulation_chunk_ids_filter(regulation_ids: list[str]):
    return or_(
        DocumentChunk.regulation_id.in_(regulation_ids),
        and_(DocumentChunk.policy_id.in_(regulation_ids), DocumentChunk.regulation_id.is_(None)),
    )


@contextmanager
def _owned_session_scope(*, commit: bool = False) -> Generator[Session, None, None]:
    """Open a short-lived session (honors gap_analysis SessionLocal monkeypatches in tests)."""
    factory = SessionLocal
    try:
        # Tests commonly patch gap_analysis_service.SessionLocal rather than app.db.session.
        from app.services import gap_analysis_service as gap_mod

        patched = getattr(gap_mod, 'SessionLocal', None)
        if patched is not None:
            factory = patched
    except Exception:
        factory = SessionLocal
    if factory is None:
        raise RuntimeError('Database session requested but DATABASE_URL is not configured')
    db = factory()
    bind_data_plane_session(db)
    try:
        yield db
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@dataclass(frozen=True)
class GovernmentExcerpt:
    policy_id: str
    policy_name: str
    regulator_source: str
    chunk_id: str
    chunk_index: int
    content: str
    similarity: float

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            'regulator_source': self.regulator_source,
            'reference_policy': self.policy_name,
            'chunk_index': self.chunk_index,
            'text': self.content[:MAX_GOVERNMENT_EXCERPT_CHARS],
            'similarity': round(self.similarity, 6),
        }


@dataclass(frozen=True)
class RegulatoryReferenceAttachment:
    policy_id: str
    policy_name: str
    regulator_source: str
    version: int
    source_url: str
    content: str

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            'policy_id': self.policy_id,
            'policy_name': self.policy_name,
            'regulator': self.regulator_source,
            'version': self.version,
            'source_url': self.source_url,
            'document_text': self.content,
        }


def regulator_source_from_policy_name(policy_name: str) -> str:
    upper = policy_name.upper()
    if 'SEC' in upper:
        return 'SEC'
    if 'FINRA' in upper:
        return 'FINRA'
    if 'CFTC' in upper:
        return 'CFTC'
    return 'REGULATOR'


async def retrieve_government_reference_excerpts(
    db: Session,
    org_id: str,
    *,
    internal_policy_text: str,
    trigger_text: str,
    settings: Settings,
) -> list[GovernmentExcerpt]:
    """Find government reference chunks most similar to the internal policy + trigger."""
    query_text = f'{trigger_text[:4_000]}\n\n{internal_policy_text[:4_000]}'.strip()
    return await retrieve_government_excerpts_for_text(
        db,
        org_id,
        query_text,
        settings,
        limit=MAX_GOVERNMENT_EXCERPTS,
    )


async def retrieve_government_excerpts_for_internal_policy(
    db: Session | None,
    org_id: str,
    *,
    internal_policy_name: str,
    internal_policy_text: str,
    trigger_text: str,
    settings: Settings,
) -> list[GovernmentExcerpt]:
    """Compare an internal policy against ALL stored FINRA/SEC/CFTC Regulations docs.

    Reserve slots per regulator so one noisy newsroom corpus (often FINRA) cannot crowd out
    SEC/CFTC excerpts before the gap-analysis prompt is built.

    Prefer ``db=None`` so embedding awaits do not pin a pool connection.
    """
    del internal_policy_name  # Analysis uses every government reference, not one regulator.
    query_text = f'{trigger_text[:4_000]}\n\n{internal_policy_text[:4_000]}'.strip()
    per_regulator = max(1, MAX_INTERNAL_POLICY_ALL_REGULATORS_EXCERPTS // len(REGULATOR_SOURCES))

    regulator_policy_ids: list[tuple[str, str]] = []
    if db is None:
        with _owned_session_scope(commit=False) as scoped_db:
            for source in REGULATOR_SOURCES:
                policy = active_reference_policy_for_regulator(scoped_db, org_id, source)
                if policy is not None:
                    regulator_policy_ids.append((source, policy.id))
    else:
        for source in REGULATOR_SOURCES:
            policy = active_reference_policy_for_regulator(db, org_id, source)
            if policy is not None:
                regulator_policy_ids.append((source, policy.id))

    query_embedding: list[float] | None = None
    embedder = optional_embedding_provider(org_id)
    if embedder is not None and query_text.strip():
        try:
            embedded = await embedder.embed(
                [query_text.strip()[:8_000]],
                trace_name='policy_comparison.query',
            )
            if embedded.vectors:
                query_embedding = embedded.vectors[0]
        except Exception:
            query_embedding = None

    retrieve_calls = [
        retrieve_government_excerpts_for_text(
            None if db is None else db,
            org_id,
            query_text,
            settings,
            limit=per_regulator,
            policy_ids=[policy_id],
            query_embedding=query_embedding,
        )
        for _source, policy_id in regulator_policy_ids
    ]
    if db is None and len(retrieve_calls) > 1:
        excerpt_lists = await asyncio.gather(*retrieve_calls)
    else:
        excerpt_lists = [await call for call in retrieve_calls]

    excerpts: list[GovernmentExcerpt] = []
    for (source, policy_id), source_excerpts in zip(
        regulator_policy_ids, excerpt_lists, strict=True
    ):
        if not source_excerpts:
            if db is None:
                with _owned_session_scope(commit=False) as scoped_db:
                    policy = (
                        scoped_db.query(Regulation)
                        .filter(Regulation.id == policy_id, Regulation.org_id == org_id)
                        .one_or_none()
                    )
                    if policy is not None:
                        source_excerpts = fallback_excerpts_from_reference_policy(
                            scoped_db, policy, source
                        )[:per_regulator]
            else:
                policy = (
                    db.query(Regulation)
                    .filter(Regulation.id == policy_id, Regulation.org_id == org_id)
                    .one_or_none()
                )
                if policy is not None:
                    source_excerpts = fallback_excerpts_from_reference_policy(
                        db, policy, source
                    )[:per_regulator]
        excerpts.extend(source_excerpts[:per_regulator])
    return merge_government_excerpts(excerpts)[:MAX_INTERNAL_POLICY_ALL_REGULATORS_EXCERPTS]


async def retrieve_government_excerpts_for_business_change(
    db: Session | None,
    org_id: str,
    business_change_text: str,
    settings: Settings,
) -> list[GovernmentExcerpt]:
    """Compare a business-change description against the latest government reference chunks.

    Prefer ``db=None`` so embedding awaits do not pin a pool connection.
    """
    excerpts = await retrieve_government_excerpts_for_text(
        None if db is None else db,
        org_id,
        business_change_text,
        settings,
        limit=MAX_BUSINESS_CHANGE_GOVERNMENT_EXCERPTS,
    )
    covered = {excerpt.regulator_source for excerpt in excerpts}
    for source in REGULATOR_SOURCES:
        if source in covered:
            continue
        regulator_excerpt = await top_government_excerpt_for_regulator(
            None if db is None else db,
            org_id,
            source,
            business_change_text,
            settings,
        )
        if regulator_excerpt is not None:
            excerpts.append(regulator_excerpt)
    return merge_government_excerpts(excerpts)[:MAX_BUSINESS_CHANGE_GOVERNMENT_EXCERPTS]


async def retrieve_government_excerpts_for_text(
    db: Session | None,
    org_id: str,
    query_text: str,
    settings: Settings,
    *,
    limit: int = MAX_GOVERNMENT_EXCERPTS,
    policy_ids: list[str] | None = None,
    query_embedding: list[float] | None = None,
) -> list[GovernmentExcerpt]:
    """Retrieve government reference chunks most similar to the supplied query text.

    Prefer ``db=None`` so OpenAI embedding awaits release the SQLAlchemy connection.
    """
    try:
        owned_sessions = db is None
        embedder = optional_embedding_provider(org_id)

        def _prep(session: Session) -> tuple[list[str], str, bool] | list[GovernmentExcerpt]:
            gov_policy_ids = policy_ids or active_government_policy_ids(session, org_id)
            if not gov_policy_ids:
                return []
            normalized_query = query_text.strip()
            if not normalized_query:
                return []
            if embedder is None:
                return lexical_government_excerpts(
                    session,
                    org_id,
                    normalized_query,
                    limit=limit,
                    policy_ids=gov_policy_ids,
                )
            has_embeddings = (
                session.query(DocumentChunk.id)
                .filter(
                    DocumentChunk.org_id == org_id,
                    _regulation_chunk_ids_filter(gov_policy_ids),
                    DocumentChunk.embedding.is_not(None),
                )
                .limit(1)
                .first()
                is not None
            )
            if not has_embeddings:
                return lexical_government_excerpts(
                    session,
                    org_id,
                    normalized_query,
                    limit=limit,
                    policy_ids=gov_policy_ids,
                )
            return gov_policy_ids, normalized_query, True

        if owned_sessions:
            with _owned_session_scope(commit=False) as scoped_db:
                prep = _prep(scoped_db)
        else:
            assert db is not None
            prep = _prep(db)

        if isinstance(prep, list):
            return prep

        gov_policy_ids, normalized_query, _ = prep
        assert embedder is not None

        # Critical: no open Session/connection across this await.
        if not query_embedding:
            embedded = await embedder.embed(
                [normalized_query[:8_000]],
                trace_name='policy_comparison.query',
            )
            query_embedding = embedded.vectors[0]

        def _query(session: Session) -> list[GovernmentExcerpt]:
            distance = DocumentChunk.embedding.cosine_distance(query_embedding)
            records = (
                session.query(DocumentChunk, Regulation, distance.label('distance'))
                .outerjoin(Regulation, Regulation.id == DocumentChunk.regulation_id)
                .filter(
                    DocumentChunk.org_id == org_id,
                    _regulation_chunk_ids_filter(gov_policy_ids),
                    DocumentChunk.embedding.isnot(None),
                    distance <= (1.0 - COSINE_RELATEDNESS_FLOOR),
                    or_(
                        DocumentChunk.document_type == REGULATORY_REFERENCE_TYPE,
                        DocumentChunk.document_type.is_(None),
                    ),
                    or_(
                        Regulation.type == REGULATORY_REFERENCE_TYPE,
                        and_(
                            DocumentChunk.regulation_id.is_(None),
                            DocumentChunk.policy_id.in_(gov_policy_ids),
                        ),
                    ),
                )
                .order_by(distance.asc())
                .limit(limit)
                .all()
            )
            results: list[GovernmentExcerpt] = []
            for chunk, regulation, raw_distance in records:
                owner = regulation
                if owner is None:
                    owner = session.query(Regulation).filter(Regulation.id == chunk.policy_id).one_or_none()
                if owner is None:
                    continue
                results.append(government_excerpt_from_record(chunk, owner, raw_distance))
            return results

        if owned_sessions:
            def _run_query() -> list[GovernmentExcerpt]:
                with _owned_session_scope(commit=False) as scoped_db:
                    return _query(scoped_db)

            return await asyncio.to_thread(_run_query)
        assert db is not None
        return _query(db)
    except Exception as exc:
        logger.warning(
            'government_reference_retrieval_failed',
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        return []


async def top_government_excerpt_for_regulator(
    db: Session | None,
    org_id: str,
    regulator_source: str,
    query_text: str,
    settings: Settings,
) -> GovernmentExcerpt | None:
    if db is None:
        with _owned_session_scope(commit=False) as scoped_db:
            policy = active_reference_policy_for_regulator(scoped_db, org_id, regulator_source)
            policy_id = policy.id if policy is not None else None
    else:
        policy = active_reference_policy_for_regulator(db, org_id, regulator_source)
        policy_id = policy.id if policy is not None else None

    if policy_id is None:
        return None

    excerpts = await retrieve_government_excerpts_for_text(
        None if db is None else db,
        org_id,
        query_text,
        settings,
        limit=MAX_GOVERNMENT_EXCERPTS,
        policy_ids=[policy_id],
    )
    if excerpts:
        return excerpts[0]

    if db is None:
        with _owned_session_scope(commit=False) as scoped_db:
            policy = (
                scoped_db.query(Regulation)
                .filter(Regulation.id == policy_id, Regulation.org_id == org_id)
                .one_or_none()
            )
            if policy is None:
                return None
            fallback = fallback_excerpts_from_reference_policy(
                scoped_db, policy, regulator_source
            )
            return fallback[0] if fallback else None

    policy = (
        db.query(Regulation)
        .filter(Regulation.id == policy_id, Regulation.org_id == org_id)
        .one_or_none()
    )
    if policy is None:
        return None
    fallback = fallback_excerpts_from_reference_policy(db, policy, regulator_source)
    return fallback[0] if fallback else None


def active_reference_policy_for_internal_name(
    db: Session,
    org_id: str,
    internal_policy_name: str,
) -> Regulation | None:
    """Resolve the latest active REGULATORY_REFERENCE row for an internal policy name."""
    regulator_source = resolve_internal_regulator_source(internal_policy_name)
    if regulator_source is None:
        return None
    return active_reference_policy_for_regulator(db, org_id, regulator_source)


def fallback_excerpts_from_reference_policy(
    db: Session,
    reference_policy: Regulation,
    regulator_source: str,
) -> list[GovernmentExcerpt]:
    """Use stored reference-policy chunks when vector retrieval returns no ranked matches."""
    chunks = (
        db.query(DocumentChunk)
        .filter(_regulation_chunk_filter(reference_policy.id))
        .order_by(DocumentChunk.chunk_index.asc())
        .limit(MAX_INTERNAL_POLICY_GOVERNMENT_EXCERPTS)
        .all()
    )
    return [
        GovernmentExcerpt(
            policy_id=reference_policy.id,
            policy_name=reference_policy.name,
            regulator_source=regulator_source,
            chunk_id=chunk.id,
            chunk_index=chunk.chunk_index,
            content=chunk.content,
            similarity=0.0,
        )
        for chunk in chunks
    ]


def active_reference_policy_for_regulator(
    db: Session,
    org_id: str,
    regulator_source: str,
) -> Regulation | None:
    from app.services.regulatory_reference_policy_service import reference_policy_names_for_source

    return (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.name.in_(reference_policy_names_for_source(regulator_source)),
        )
        .order_by(Regulation.version.desc())
        .first()
    )


def reference_policy_name_for_source(regulator_source: str) -> str:
    from app.services.regulatory_reference_policy_service import reference_policy_name

    return reference_policy_name(regulator_source)


def active_government_policy_ids(db: Session, org_id: str) -> list[str]:
    return [
        policy_id
        for (policy_id,) in db.query(Regulation.id)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.rule_code.is_(None),
        )
        .all()
    ]


def load_regulatory_reference_policy_text(db: Session, policy: Regulation) -> str:
    """Load the full text for one REGULATORY_REFERENCE policy from storage or embedded chunks."""
    if policy.file_url and policy.file_url.startswith('storage://'):
        try:
            from app.services.redline_document import extract_policy_paragraphs, policy_text_from_paragraphs
            from app.services.storage_service import download_file

            raw = download_file(policy.file_url)
            extracted = policy_text_from_paragraphs(extract_policy_paragraphs(raw)).strip()
            if extracted:
                return extracted
        except Exception as exc:
            logger.warning(
                'regulatory_reference_storage_download_failed',
                policy_id=policy.id,
                policy_name=policy.name,
                error_type=type(exc).__name__,
            )

    chunks = (
        db.query(DocumentChunk)
        .filter(_regulation_chunk_filter(policy.id))
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    return '\n\n'.join(
        chunk.content.strip() for chunk in chunks if (chunk.content or '').strip()
    ).strip()


def load_regulatory_reference_attachments(
    db: Session,
    org_id: str,
) -> list[RegulatoryReferenceAttachment]:
    """Load every active REGULATORY_REFERENCE policy for prompt support attachments."""
    policies = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.rule_code.is_(None),
        )
        .order_by(Regulation.name.asc(), Regulation.version.desc())
        .all()
    )

    attachments: list[RegulatoryReferenceAttachment] = []
    seen_names: set[str] = set()
    for policy in policies:
        if policy.name in seen_names:
            continue
        seen_names.add(policy.name)

        content = load_regulatory_reference_policy_text(db, policy)
        if not content:
            logger.warning(
                'regulatory_reference_attachment_empty',
                org_id=org_id,
                policy_id=policy.id,
                policy_name=policy.name,
            )
            continue

        attachments.append(
            RegulatoryReferenceAttachment(
                policy_id=policy.id,
                policy_name=policy.name,
                regulator_source=regulator_source_from_policy_name(policy.name),
                version=policy.version,
                source_url=(policy.file_url or '').strip(),
                content=content[:MAX_REGULATORY_REFERENCE_ATTACHMENT_CHARS],
            )
        )

    logger.info(
        'regulatory_reference_attachments_loaded',
        org_id=org_id,
        attachment_count=len(attachments),
        policy_names=[attachment.policy_name for attachment in attachments],
    )
    return attachments


def government_excerpt_from_record(
    chunk: DocumentChunk,
    policy: Regulation,
    raw_distance: float | None,
) -> GovernmentExcerpt:
    similarity = max(0.0, 1.0 - float(raw_distance or 1.0))
    return GovernmentExcerpt(
        policy_id=policy.id,
        policy_name=policy.name,
        regulator_source=regulator_source_from_policy_name(policy.name),
        chunk_id=chunk.id,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        similarity=similarity,
    )


def merge_government_excerpts(
    *groups: Iterable[GovernmentExcerpt],
) -> list[GovernmentExcerpt]:
    merged: dict[str, GovernmentExcerpt] = {}
    for group in groups:
        for excerpt in group:
            existing = merged.get(excerpt.chunk_id)
            if existing is None or excerpt.similarity > existing.similarity:
                merged[excerpt.chunk_id] = excerpt
    return sorted(merged.values(), key=lambda excerpt: excerpt.similarity, reverse=True)


def lexical_government_excerpts(
    db: Session,
    org_id: str,
    query_text: str,
    *,
    limit: int = MAX_GOVERNMENT_EXCERPTS,
    policy_ids: list[str] | None = None,
) -> list[GovernmentExcerpt]:
    query_terms = {term for term in query_text.lower().split() if len(term) >= 4}
    if not query_terms:
        return []

    query = (
        db.query(DocumentChunk, Regulation)
        .join(
            Regulation,
            Regulation.id == func.coalesce(DocumentChunk.regulation_id, DocumentChunk.policy_id),
        )
        .filter(
            DocumentChunk.org_id == org_id,
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
        )
    )
    if policy_ids:
        query = query.filter(_regulation_chunk_ids_filter(policy_ids))
    records = query.all()
    ranked: list[tuple[DocumentChunk, Regulation, float]] = []
    for chunk, policy in records:
        text = chunk.content.lower()
        matched = sum(1 for term in query_terms if term in text)
        if matched:
            ranked.append((chunk, policy, matched / len(query_terms)))
    ranked.sort(key=lambda item: item[2], reverse=True)

    return [
        government_excerpt_from_record(chunk, policy, 1.0 - score)
        for chunk, policy, score in ranked[:limit]
    ]
