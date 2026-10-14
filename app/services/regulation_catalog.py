"""Catalog of cited government marketing rules stored in ``regulations``."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.core import Regulation
from app.services.policy_types import REGULATORY_REFERENCE_TYPE

MARKETING_REGULATION_SEED: tuple[dict[str, Any], ...] = (
    {
        'rule_code': '206(4)-1',
        'source': 'SEC',
        'name': 'SEC: Rule 206(4)-1: The Marketing Rule',
        'aliases': ('206(4)-1', '206(4)1'),
    },
    {
        'rule_code': '204-2',
        'source': 'SEC',
        'name': 'SEC: Rule 204-2: Recordkeeping',
        'aliases': ('204-2',),
    },
    {
        'rule_code': '2210',
        'source': 'FINRA',
        'name': 'FINRA: Rule 2210: Communications with the public',
        'aliases': ('2210', '2010'),
    },
    {
        'rule_code': '2211-2216',
        'source': 'FINRA',
        'name': 'FINRA: Rules 2211–2216',
        'aliases': ('2211', '2212', '2213', '2214', '2215', '2216'),
    },
)


def marketing_regulation_id(org_id: str, rule_code: str) -> str:
    digest = hashlib.md5(f'{org_id}:{rule_code}'.encode(), usedforsecurity=False).hexdigest()
    return f'mkt-{digest}'


def ensure_marketing_regulations(db: Session, org_id: str) -> list[Regulation]:
    """Upsert the four required SEC/FINRA marketing rules for one org."""
    normalized_org = (org_id or '').strip()
    if not normalized_org:
        return []

    now = datetime.now(UTC).replace(tzinfo=None)
    rows: list[Regulation] = []
    for item in MARKETING_REGULATION_SEED:
        rule_code = str(item['rule_code'])
        existing = (
            db.query(Regulation)
            .filter(
                Regulation.org_id == normalized_org,
                Regulation.rule_code == rule_code,
            )
            .order_by(Regulation.version.desc())
            .first()
        )
        if existing is not None:
            if existing.name != item['name'] or existing.source != item['source']:
                existing.name = str(item['name'])
                existing.source = str(item['source'])
                existing.type = REGULATORY_REFERENCE_TYPE
                existing.status = 'ACTIVE'
                existing.updated_at = now
            rows.append(existing)
            continue
        row = Regulation(
            id=marketing_regulation_id(normalized_org, rule_code),
            org_id=normalized_org,
            name=str(item['name']),
            type=REGULATORY_REFERENCE_TYPE,
            version=1,
            status='ACTIVE',
            source=str(item['source']),
            rule_code=rule_code,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows
