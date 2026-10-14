"""Sync scraped regulator content into org-scoped Policy Library reference documents."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import structlog
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import embedding_model_label
from app.core.config import Settings
from app.db.ordering import desc
from app.models.core import DocumentChunk, Organization, Policy, Regulation, RegulatoryPublication
from app.services.document_chunk_service import (
    chunk_policy_name_value,
    delete_chunks_for_regulation_ids,
    purge_superseded_policy_chunks,
)
from app.services.document_ingestion import TextChunk, chunk_publication_text, generate_embeddings
from app.services.policy_types import resolve_internal_regulator_source
from app.services.regulation_catalog import ensure_marketing_regulations

logger = structlog.get_logger(__name__)

REGULATORY_REFERENCE_TYPE = 'REGULATORY_REFERENCE'

REFERENCE_POLICY_NAMES: dict[str, str] = {
    'SEC': 'SEC Regulations',
    'FINRA': 'FINRA Regulations',
    'CFTC': 'CFTC Regulations',
}

# Older synced titles that should be superseded when creating the new regulation docs.
LEGACY_REFERENCE_POLICY_NAMES: dict[str, tuple[str, ...]] = {
    'SEC': ('SEC Latest Regulations', 'SEC Newsroom Reference'),
    'FINRA': ('FINRA Latest Regulations', 'FINRA Media Center Reference'),
    'CFTC': ('CFTC Latest Regulations', 'CFTC Press Room Reference'),
}

REFERENCE_SOURCE_URLS: dict[str, str] = {
    'SEC': 'https://www.sec.gov/newsroom',
    'FINRA': 'https://www.finra.org/media-center',
    'CFTC': 'https://www.cftc.gov/PressRoom/index.htm',
}

REFERENCE_SOURCES = frozenset(REFERENCE_POLICY_NAMES)
ALL_REFERENCE_SOURCES: tuple[str, ...] = ('FINRA', 'SEC', 'CFTC')
DOCX_CONTENT_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
DEFAULT_REGULATORY_REFERENCE_MAX_VERSIONS = 10


def list_publications_for_reference(
    db: Session,
    source: str,
    *,
    limit: int,
) -> list[RegulatoryPublication]:
    """Newest publications for a regulator reference document.

    SEC Regulations use newsroom/rules pages only — company EDGAR filings are
    excluded so issuer 10-K/8-K noise cannot crowd out press releases.
    """
    normalized = source.strip().upper()
    query = db.query(RegulatoryPublication).filter(
        RegulatoryPublication.source == normalized
    )
    if normalized == 'SEC':
        # Match case-insensitive Archives/edgar path segments from sec.gov.
        query = query.filter(~RegulatoryPublication.url.ilike('%/Archives/edgar/%'))
    return (
        query.order_by(
            RegulatoryPublication.published_at.desc().nullslast(),
            RegulatoryPublication.created_at.desc(),
        )
        .limit(limit)
        .all()
    )


def reference_policy_name(source: str) -> str:
    normalized = source.strip().upper()
    return REFERENCE_POLICY_NAMES.get(normalized, f'{normalized} Regulations')


def reference_policy_names_for_source(source: str) -> list[str]:
    """Return current + legacy policy names that identify the same regulator reference."""
    normalized = source.strip().upper()
    names = [reference_policy_name(normalized)]
    names.extend(LEGACY_REFERENCE_POLICY_NAMES.get(normalized, ()))
    return names


def prune_regulatory_reference_versions(
    db: Session,
    *,
    org_id: str,
    source: str,
    keep: int = DEFAULT_REGULATORY_REFERENCE_MAX_VERSIONS,
) -> int:
    """Keep at most ``keep`` REGULATORY_REFERENCE rows for a regulator source.

    Newest versions win. Exactly one ACTIVE is preferred; any extra ACTIVE rows
    outside the keep window are demoted to SUPERSEDED then eligible for delete.
    """
    if keep < 1:
        keep = 1
    names = reference_policy_names_for_source(source)
    policies = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.name.in_(names),
        )
        .order_by(Regulation.version.desc(), Regulation.updated_at.desc().nullslast())
        .all()
    )
    if len(policies) <= keep:
        return 0

    # Newest ``keep`` rows. Prefer including the newest ACTIVE inside that window.
    keep_list = list(policies[:keep])
    keep_ids = {policy.id for policy in keep_list}
    newest_active = next((policy for policy in policies if policy.status == 'ACTIVE'), None)
    if newest_active is not None and newest_active.id not in keep_ids:
        # Swap the oldest kept non-active row for the ACTIVE row.
        for index in range(len(keep_list) - 1, -1, -1):
            if keep_list[index].status != 'ACTIVE':
                keep_ids.discard(keep_list[index].id)
                keep_ids.add(newest_active.id)
                break

    # Demote any ACTIVE rows that fall outside the keep set so they can be deleted.
    for policy in policies:
        if policy.id not in keep_ids and policy.status == 'ACTIVE':
            policy.status = 'SUPERSEDED'

    delete_ids = [policy.id for policy in policies if policy.id not in keep_ids]
    if not delete_ids:
        return 0

    delete_chunks_for_regulation_ids(db, delete_ids)
    deleted = (
        db.query(Regulation)
        .filter(Regulation.id.in_(delete_ids))
        .delete(synchronize_session=False)
    )
    deleted_count = int(deleted or 0)
    if deleted_count:
        logger.info(
            'pruned_regulatory_reference_versions',
            org_id=org_id,
            source=source,
            kept=len(keep_ids),
            deleted=deleted_count,
            max_versions=keep,
        )
    return deleted_count


def prune_all_regulatory_reference_versions_for_org(
    db: Session,
    *,
    org_id: str,
    keep: int = DEFAULT_REGULATORY_REFERENCE_MAX_VERSIONS,
) -> int:
    """Enforce the version cap for FINRA/SEC/CFTC reference docs in one org."""
    deleted = 0
    for source in ALL_REFERENCE_SOURCES:
        deleted += prune_regulatory_reference_versions(
            db,
            org_id=org_id,
            source=source,
            keep=keep,
        )
    return deleted


def build_reference_docx_bytes(policy_name: str, document_text: str) -> bytes:
    """Create a downloadable Word document for scraped government regulations."""
    from io import BytesIO

    from docx import Document

    document = Document()
    document.add_heading(policy_name, level=1)
    document.add_paragraph(
        'Government regulation content scraped for Reg AI gap analysis. '
        'This document is decision support and not legal advice.'
    )
    for block in document_text.split('\n\n'):
        text = block.strip()
        if not text:
            continue
        if text.startswith('## '):
            document.add_heading(text[3:].strip(), level=2)
            continue
        document.add_paragraph(text)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def store_reference_policy_file(
    *,
    org_id: str,
    policy_id: str,
    policy_name: str,
    document_text: str,
) -> str | None:
    """Upload the Regulations DOCX and return a storage URI when successful."""
    try:
        from app.services.policy_service import store_policy_file

        safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', policy_name).strip('._') or 'regulations'
        stored = store_policy_file(
            org_id=org_id,
            policy_id=policy_id,
            filename=f'{safe_name}.docx',
            contents=build_reference_docx_bytes(policy_name, document_text),
            content_type=DOCX_CONTENT_TYPE,
        )
        return stored.uri
    except Exception as exc:
        logger.warning(
            'regulatory_reference_file_upload_failed',
            org_id=org_id,
            policy_id=policy_id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return None


def reference_source_url(source: str) -> str:
    normalized = source.strip().upper()
    return REFERENCE_SOURCE_URLS.get(normalized, '')


def org_reference_sources(organization: Organization, db: Session | None = None) -> list[str]:
    """Return FINRA, SEC, and CFTC for every org.

    Internal policies use free-text names and are always analyzed against all three
    government regulation documents, so Policy Library Latest Regulations stay in sync
    regardless of internal policy naming.
    """
    del organization, db
    return list(ALL_REFERENCE_SOURCES)


def regulator_sources_for_policy_ids(
    db: Session,
    org_id: str,
    policy_ids: Iterable[str],
) -> list[str]:
    """Return FINRA/SEC/CFTC sources for the uploaded internal policies being processed."""
    ids = [str(policy_id).strip() for policy_id in policy_ids if str(policy_id).strip()]
    if not ids:
        return []
    rows = (
        db.query(Policy.name)
        .filter(
            Policy.org_id == org_id,
            Policy.id.in_(ids),
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .all()
    )
    sources: set[str] = set()
    for (name,) in rows:
        regulator = resolve_internal_regulator_source(str(name))
        if regulator is not None:
            sources.add(regulator)
    return sorted(sources)


def collect_regulatory_scrape_sources(
    db: Session,
    org_id: str | None = None,
) -> list[str]:
    """Return FINRA, SEC, and CFTC — always scrape all government regulation sources."""
    del db, org_id
    return list(ALL_REFERENCE_SOURCES)


def active_internal_regulator_sources(db: Session, org_id: str) -> set[str]:
    """Return FINRA/SEC/CFTC sources for active uploaded internal enterprise policies."""
    rows = (
        db.query(Policy.name)
        .filter(
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .all()
    )
    sources: set[str] = set()
    for (name,) in rows:
        regulator = resolve_internal_regulator_source(str(name))
        if regulator is not None:
            sources.add(regulator)
    return sources


def build_reference_document_text(publications: list[RegulatoryPublication]) -> str:
    sections: list[str] = []
    for publication in publications:
        published = (
            publication.published_at.astimezone(UTC).date().isoformat()
            if publication.published_at
            else 'Unknown date'
        )
        body = (publication.raw_text or '').strip() or 'Full text pending scrape.'
        sections.append(
            '\n'.join(
                [
                    f'## {publication.title}',
                    f'Source URL: {publication.url}',
                    f'Published: {published}',
                    '',
                    body,
                ]
            )
        )
    return '\n\n---\n\n'.join(sections)


def build_bootstrap_reference_text(source: str) -> str:
    """Placeholder document so every scoped regulator appears in the Policy Library."""
    policy_name = reference_policy_name(source)
    source_url = reference_source_url(source) or 'the regulator website'
    return (
        f'# {policy_name}\n\n'
        f'This government reference document is active for {source}. '
        f'Scraped publications will replace this placeholder after the next successful sync.\n\n'
        f'Source: {source_url}\n'
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def next_regulation_version(db: Session, org_id: str, regulation_name: str) -> int:
    max_version = (
        db.query(func.max(Regulation.version))
        .filter(Regulation.org_id == org_id, Regulation.name == regulation_name)
        .scalar()
    )
    return int(max_version or 0) + 1


def active_reference_policy(db: Session, org_id: str, policy_name: str) -> Regulation | None:
    return (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.name == policy_name,
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.status == 'ACTIVE',
        )
        .order_by(desc(Regulation.version))
        .first()
    )


def active_reference_content_hash(db: Session, policy_id: str) -> str | None:
    chunks = (
        db.query(DocumentChunk)
        .filter(
            (DocumentChunk.regulation_id == policy_id)
            | ((DocumentChunk.policy_id == policy_id) & DocumentChunk.regulation_id.is_(None))
        )
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    if not chunks:
        return None
    return content_hash('\n'.join(chunk.content for chunk in chunks))


async def sync_regulatory_reference_policies(
    db: Session,
    sources: Iterable[str] | None,
    settings: Settings,
    *,
    org_id: str | None = None,
) -> int:
    """Create or version org reference policies for scoped SEC/FINRA/CFTC sources.

    Prefer ``sync_regulatory_reference_policies_releasing`` from upload/scrape workers so
    OpenAI embedding calls do not hold a pooled SQLAlchemy connection.
    """
    planned = _plan_regulatory_reference_syncs(
        db, sources, settings, org_id=org_id
    )
    for item in planned:
        item.embeddings = await generate_embeddings(item.chunks, settings, org_id=item.target_org_id)
    return _apply_regulatory_reference_syncs(db, planned, settings)


@dataclass
class _ReferenceSyncPlan:
    target_org_id: str
    source: str
    policy_name: str
    document_text: str
    document_hash: str
    source_url: str
    publication_count: int
    chunks: list[TextChunk]
    embeddings: list[list[float]] = field(default_factory=list)


def _plan_regulatory_reference_syncs(
    db: Session,
    sources: Iterable[str] | None,
    settings: Settings,
    *,
    org_id: str | None = None,
) -> list[_ReferenceSyncPlan]:
    """Inspect DB state and return version-create plans (no embedding / no writes yet)."""
    allowed_sources = (
        {str(source).strip().upper() for source in sources if str(source).strip()}
        if sources is not None
        else None
    )

    organizations_query = db.query(Organization)
    if org_id is not None:
        organizations_query = organizations_query.filter(Organization.auth_org_id == org_id)
    organizations = organizations_query.all()
    if not organizations:
        return []

    planned: list[_ReferenceSyncPlan] = []
    for organization in organizations:
        target_org_id = organization.auth_org_id
        org_sources = org_reference_sources(organization, db)
        if allowed_sources is not None:
            org_sources = [source for source in org_sources if source in allowed_sources]
        for source in org_sources:
            publications = list_publications_for_reference(
                db,
                source,
                limit=settings.regulatory_max_items_per_feed,
            )
            document_text = (
                build_reference_document_text(publications)
                if publications
                else build_bootstrap_reference_text(source)
            )
            document_hash = content_hash(document_text)
            policy_name = reference_policy_name(source)
            source_url = reference_source_url(source) or (
                publications[0].url if publications else ''
            )

            current = active_reference_policy(db, target_org_id, policy_name)
            if current is None:
                for legacy_name in LEGACY_REFERENCE_POLICY_NAMES.get(source, ()):
                    current = active_reference_policy(db, target_org_id, legacy_name)
                    if current is not None:
                        break
            if current is not None:
                current_hash = active_reference_content_hash(db, current.id)
                if current_hash == document_hash and current.name == policy_name:
                    if not current.file_url or str(current.file_url).startswith('http'):
                        stored_file_url = store_reference_policy_file(
                            org_id=target_org_id,
                            policy_id=current.id,
                            policy_name=policy_name,
                            document_text=document_text,
                        )
                        if stored_file_url:
                            current.file_url = stored_file_url
                            current.updated_at = datetime.now(UTC)
                            try:
                                db.commit()
                            except SQLAlchemyError:
                                db.rollback()
                    logger.info(
                        'regulatory_reference_policy_unchanged',
                        org_id=target_org_id,
                        source=source,
                        policy_name=policy_name,
                        policy_id=current.id,
                    )
                    pruned = prune_regulatory_reference_versions(
                        db,
                        org_id=target_org_id,
                        source=source,
                        keep=settings.regulatory_reference_max_versions,
                    )
                    if pruned:
                        try:
                            db.commit()
                        except SQLAlchemyError:
                            db.rollback()
                    continue

            chunks = chunk_publication_text(policy_name, document_text)
            planned.append(
                _ReferenceSyncPlan(
                    target_org_id=target_org_id,
                    source=source,
                    policy_name=policy_name,
                    document_text=document_text,
                    document_hash=document_hash,
                    source_url=source_url,
                    publication_count=len(publications),
                    chunks=chunks,
                )
            )
    return planned


def _apply_regulatory_reference_syncs(
    db: Session,
    planned: list[_ReferenceSyncPlan],
    settings: Settings,
) -> int:
    """Persist new REGULATORY_REFERENCE policy versions from precomputed embeddings."""
    updated = 0
    for item in planned:
        version = next_regulation_version(db, item.target_org_id, item.policy_name)
        policy_id = str(uuid4())
        stored_file_url = store_reference_policy_file(
            org_id=item.target_org_id,
            policy_id=policy_id,
            policy_name=item.policy_name,
            document_text=item.document_text,
        )
        file_url = stored_file_url or item.source_url
        try:
            for name_to_supersede in reference_policy_names_for_source(item.source):
                db.query(Regulation).filter(
                    Regulation.org_id == item.target_org_id,
                    Regulation.name == name_to_supersede,
                    Regulation.type == REGULATORY_REFERENCE_TYPE,
                    Regulation.status == 'ACTIVE',
                    Regulation.rule_code.is_(None),
                ).update({Regulation.status: 'SUPERSEDED'}, synchronize_session=False)
            purge_superseded_policy_chunks(db, org_id=item.target_org_id)
            db.add(
                Regulation(
                    id=policy_id,
                    org_id=item.target_org_id,
                    name=item.policy_name,
                    type=REGULATORY_REFERENCE_TYPE,
                    version=version,
                    status='ACTIVE',
                    file_url=file_url,
                    source=item.source,
                    updated_at=datetime.now(UTC),
                )
            )
            db.flush()
            persist_reference_chunks(
                db=db,
                org_id=item.target_org_id,
                policy_id=policy_id,
                chunks=item.chunks,
                embeddings=item.embeddings,
                embedding_model=embedding_model_label(item.target_org_id, settings),
                source=item.source,
                content_hash=item.document_hash,
            )
            db.commit()
            updated += 1
            prune_regulatory_reference_versions(
                db,
                org_id=item.target_org_id,
                source=item.source,
                keep=settings.regulatory_reference_max_versions,
            )
            try:
                db.commit()
            except SQLAlchemyError:
                db.rollback()
            logger.info(
                'regulatory_reference_policy_synced',
                org_id=item.target_org_id,
                source=item.source,
                policy_name=item.policy_name,
                version=version,
                publication_count=item.publication_count,
                downloadable=bool(stored_file_url),
            )
        except SQLAlchemyError as exc:
            db.rollback()
            logger.exception(
                'regulatory_reference_policy_sync_failed',
                org_id=item.target_org_id,
                source=item.source,
                error_type=type(exc).__name__,
            )
            del exc
    return updated


async def sync_regulatory_reference_policies_releasing(
    sources: Iterable[str] | None,
    settings: Settings,
    *,
    org_id: str | None = None,
) -> int:
    """Plan → embed (no DB) → write, so embeddings never hold a pool connection."""
    from app.db.session import session_scope

    with session_scope(commit=True, org_id=org_id) as scoped:
        planned = _plan_regulatory_reference_syncs(
            scoped, sources, settings, org_id=org_id
        )
    for item in planned:
        item.embeddings = await generate_embeddings(item.chunks, settings, org_id=item.target_org_id)
    if not planned:
        return 0
    with session_scope(commit=True, org_id=org_id) as scoped:
        return _apply_regulatory_reference_syncs(scoped, planned, settings)


async def refresh_government_reference_policies_for_org(
    db: Session,
    org_id: str,
    settings: Settings,
    *,
    sources: Iterable[str] | None = None,
) -> int:
    """Refresh org-scoped government reference policies from the latest scraped publications."""
    _ensure_organization_for_reference_sync(db, org_id)
    updated = await sync_regulatory_reference_policies(
        db,
        sources if sources is not None else ALL_REFERENCE_SOURCES,
        settings,
        org_id=org_id,
    )
    # Always enforce the version cap, including for historical backlog when
    # scrape content did not change and no new version was created.
    pruned = prune_all_regulatory_reference_versions_for_org(
        db,
        org_id=org_id,
        keep=settings.regulatory_reference_max_versions,
    )
    if pruned:
        try:
            db.commit()
            logger.info(
                'regulatory_reference_versions_pruned_for_org',
                org_id=org_id,
                deleted=pruned,
                max_versions=settings.regulatory_reference_max_versions,
            )
        except SQLAlchemyError:
            db.rollback()
    return updated


async def refresh_government_reference_policies_for_org_releasing(
    org_id: str,
    settings: Settings,
    *,
    sources: Iterable[str] | None = None,
) -> int:
    """Like refresh_government_reference_policies_for_org, but never holds DB across embeds."""
    from app.db.session import session_scope

    selected = sources if sources is not None else ALL_REFERENCE_SOURCES
    with session_scope(commit=True, org_id=org_id) as scoped:
        _ensure_organization_for_reference_sync(scoped, org_id)
        planned = _plan_regulatory_reference_syncs(
            scoped, selected, settings, org_id=org_id
        )
    for item in planned:
        item.embeddings = await generate_embeddings(item.chunks, settings, org_id=item.target_org_id)
    updated = 0
    with session_scope(commit=True, org_id=org_id) as scoped:
        if planned:
            updated = _apply_regulatory_reference_syncs(scoped, planned, settings)
        pruned = prune_all_regulatory_reference_versions_for_org(
            scoped,
            org_id=org_id,
            keep=settings.regulatory_reference_max_versions,
        )
        if pruned:
            try:
                scoped.commit()
                logger.info(
                    'regulatory_reference_versions_pruned_for_org',
                    org_id=org_id,
                    deleted=pruned,
                    max_versions=settings.regulatory_reference_max_versions,
                )
            except SQLAlchemyError:
                scoped.rollback()
    return updated


def _ensure_organization_for_reference_sync(db: Session, org_id: str) -> Organization:
    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == org_id)
        .one_or_none()
    )
    if organization is None:
        # Policy uploads can run before an Organization row exists; still materialize
        # FINRA/SEC/CFTC Regulations docs so Latest Regulations and gap analysis work.
        organization = Organization(
            id=str(uuid4()),
            auth_org_id=org_id,
            name=org_id,
            regulatory_scope={'sources': list(ALL_REFERENCE_SOURCES)},
        )
        db.add(organization)
        db.flush()
        logger.info(
            'regulatory_reference_org_bootstrapped',
            org_id=org_id,
        )
    ensure_marketing_regulations(db, org_id)
    return organization


def persist_reference_chunks(
    db: Session,
    org_id: str,
    policy_id: str,
    chunks: list[TextChunk],
    embeddings: list[list[float]],
    embedding_model: str,
    source: str,
    content_hash: str,
) -> None:
    for chunk in chunks:
        embedding = embeddings[chunk.index] if embeddings else None
        db.add(
            DocumentChunk(
                id=str(uuid4()),
                org_id=org_id,
                policy_id=None,
                regulation_id=policy_id,
                chunk_index=chunk.index,
                content=chunk.content,
                document_type=REGULATORY_REFERENCE_TYPE,
                policy_name=chunk_policy_name_value(regulator_source=source),
                embedding=embedding,
                metadata_json={
                    **chunk.metadata,
                    'source': source,
                    'reference_type': REGULATORY_REFERENCE_TYPE,
                    'policy_type': REGULATORY_REFERENCE_TYPE,
                    'content_hash': content_hash,
                    'embedding_model': embedding_model if embedding is not None else None,
                    'embedded': embedding is not None,
                },
            )
        )
