"""Mode B: recheck archived marketing only when a rule change touches marketing."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.session import session_scope
from app.models.core import MarketingContent, RegulatoryPublication
from app.services.marketing_review_workflow import run_marketing_review
from app.services.marketing_rule_store import publication_touches_marketing
from app.services.marketing_types import MAX_ARCHIVE_RECHECK

logger = structlog.get_logger(__name__)


def marketing_tags_for_publication(publication: RegulatoryPublication) -> list[str]:
    blob = f'{publication.title} {publication.raw_text or ""}'.lower()
    tags: list[str] = []
    if 'testimonial' in blob or 'endorsement' in blob:
        tags.append('testimonial')
    if 'performance' in blob or 'hypothetical' in blob:
        tags.append('performance')
    if 'hypothetical' in blob or 'backtest' in blob:
        tags.append('hypothetical_performance')
    if 'rating' in blob or 'ranking' in blob:
        tags.append('third_party_rating')
    if '2210' in blob or 'communications with the public' in blob:
        tags.extend(['structured_products', 'scarcity'])
    return list(dict.fromkeys(tags))


def candidate_archive_ids(db: Session, *, org_id: str, tags: list[str]) -> list[str]:
    query = db.query(MarketingContent.id).filter(MarketingContent.org_id == org_id)
    if tags:
        query = query.filter(or_(*[
            MarketingContent.content_tags.contains([tag])
            for tag in tags
        ]))
    rows = query.order_by(MarketingContent.created_at.desc()).limit(MAX_ARCHIVE_RECHECK).all()
    return [row[0] for row in rows]


async def run_mode_b_after_scrape(org_id: str) -> dict[str, Any]:
    """Called after a scrape that created publications. No-op if none touch marketing."""
    with session_scope(commit=False, org_id=org_id) as db:
        recent = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.source.in_(('SEC', 'FINRA')))
            .order_by(RegulatoryPublication.created_at.desc())
            .limit(12)
            .all()
        )
        marketing = [row for row in recent if publication_touches_marketing(row)]
    if not marketing:
        return {'status': 'skipped', 'reason': 'no_marketing_rule_change', 'rechecked': 0}
    return await run_mode_b_for_org(org_id, publication_id=marketing[0].id)


async def run_mode_b_for_org(org_id: str, *, publication_id: str | None = None) -> dict[str, Any]:
    """Recheck tagged archive pieces when a marketing-related rule changed."""
    with session_scope(commit=False, org_id=org_id) as db:
        if publication_id:
            publication = (
                db.query(RegulatoryPublication)
                .filter(RegulatoryPublication.id == publication_id)
                .one_or_none()
            )
            if publication is None or not publication_touches_marketing(publication):
                return {
                    'status': 'skipped',
                    'reason': 'publication_not_marketing',
                    'rechecked': 0,
                    'skipped_archive': 0,
                }
            tags = marketing_tags_for_publication(publication)
        else:
            publication = None
            tags = ['performance', 'testimonial', 'third_party_rating', 'hypothetical_performance']
        total = (
            db.query(MarketingContent.id)
            .filter(MarketingContent.org_id == org_id)
            .count()
        )
        content_ids = candidate_archive_ids(db, org_id=org_id, tags=tags)

    rechecked = 0
    errors: list[str] = []
    for content_id in content_ids:
        result = await run_marketing_review(
            org_id=org_id,
            content_id=content_id,
            mode='B',
            trigger_type='publication',
            trigger_id=publication_id,
            publication_id=publication_id,
        )
        if result.get('status') == 'failed':
            errors.append(content_id)
        else:
            rechecked += 1
    skipped = max(int(total) - len(content_ids), 0)
    logger.info(
        'marketing_mode_b_completed',
        org_id=org_id,
        publication_id=publication_id,
        rechecked=rechecked,
        skipped_archive=skipped,
    )
    return {
        'status': 'completed' if not errors else 'partial',
        'rechecked': rechecked,
        'skipped_archive': skipped,
        'errors': errors,
    }
