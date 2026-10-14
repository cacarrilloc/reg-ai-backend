"""Read the shared R1 rule store for marketing reviews. Never hard-codes rule text."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.models.core import Regulation, RegulatoryPublication
from app.services.marketing_types import MARKETING_RULE_KEYWORDS, MAX_RULE_EXCERPT_CHARS
from app.services.policy_types import REGULATORY_REFERENCE_TYPE
from app.services.regulation_catalog import ensure_marketing_regulations


def publication_touches_marketing(publication: RegulatoryPublication | None) -> bool:
    if publication is None:
        return False
    blob = ' '.join(
        part for part in (
            publication.source,
            publication.title,
            publication.url,
            (publication.raw_text or '')[:4_000],
        ) if part
    ).lower()
    return any(keyword in blob for keyword in MARKETING_RULE_KEYWORDS)


def load_marketing_rule_bundle(db: Session, *, org_id: str) -> dict[str, Any]:
    """Current SEC/FINRA marketing excerpts plus a version stamp for 204-2."""
    publications = (
        db.query(RegulatoryPublication)
        .filter(RegulatoryPublication.source.in_(('SEC', 'FINRA')))
        .order_by(RegulatoryPublication.published_at.desc().nullslast(), RegulatoryPublication.created_at.desc())
        .limit(40)
        .all()
    )
    marketing_pubs = [row for row in publications if publication_touches_marketing(row)]
    ensure_marketing_regulations(db, org_id)
    references = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.name.in_(('SEC Regulations', 'FINRA Regulations')),
        )
        .all()
    )
    marketing_rules = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == 'ACTIVE',
            Regulation.rule_code.is_not(None),
        )
        .order_by(Regulation.source.asc(), Regulation.rule_code.asc())
        .all()
    )
    excerpts: list[dict[str, str]] = []
    version_parts: list[str] = []
    for row in marketing_pubs[:12]:
        text = (row.raw_text or row.title or '')[:2_000]
        excerpts.append({
            'source': row.source,
            'title': row.title,
            'url': row.url or '',
            'text': text,
        })
        version_parts.append(f'{row.id}:{row.created_at}')
    for policy in references:
        excerpts.append({
            'source': 'reference_policy',
            'title': policy.name,
            'url': '',
            'text': f'{policy.name} v{policy.version}',
        })
        version_parts.append(f'{policy.id}:{policy.version}:{policy.updated_at}')
    for rule in marketing_rules:
        excerpts.append({
            'source': rule.source or 'regulation',
            'title': rule.name,
            'url': '',
            'text': f'{rule.name} ({rule.rule_code})',
        })
        version_parts.append(f'{rule.id}:{rule.rule_code}:{rule.updated_at}')
    joined = '\n\n'.join(f'{item["title"]}\n{item["text"]}' for item in excerpts)[:MAX_RULE_EXCERPT_CHARS]
    digest = hashlib.sha256('|'.join(version_parts).encode('utf-8')).hexdigest()[:16] if version_parts else 'empty'
    return {
        'excerpts': excerpts,
        'excerpt_text': joined,
        'publication_ids': [row.id for row in marketing_pubs[:12]],
        'reference_policy_ids': [policy.id for policy in references],
        'regulation_ids': [rule.id for rule in marketing_rules],
        'version': digest,
        'unvalidated': True if marketing_pubs else False,
    }
