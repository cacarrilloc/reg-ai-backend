"""Re-evaluate policy documents against recent regulatory and business-change triggers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.models.core import BusinessChange, Organization, Policy, RegulatoryPublication
from app.schemas.agent import AgentRunResponse
from app.services.gap_analysis_service import run_gap_analysis
from app.services.regulatory_monitoring_service import publication_matches_organization
from app.services.workflow_dispatch_service import workflow_agent_request

logger = structlog.get_logger(__name__)


async def evaluate_policy_against_triggers(
    policy_id: str,
    org_id: str,
    db: Session,
    settings: Settings | None = None,
) -> list[AgentRunResponse]:
    """Run gap analysis for one policy against recent publications and business changes."""
    settings = settings or get_settings()
    policy = (
        db.query(Policy)
        .filter(Policy.id == policy_id, Policy.org_id == org_id, Policy.status == 'ACTIVE')
        .one_or_none()
    )
    if policy is None:
        logger.warning('policy_re_evaluation_skipped', reason='policy_not_found', policy_id=policy_id)
        return []

    organization = db.query(Organization).filter(Organization.auth_org_id == org_id).one_or_none()
    if organization is None:
        return []

    lookback = datetime.now(UTC) - timedelta(days=settings.policy_re_evaluation_lookback_days)
    responses: list[AgentRunResponse] = []

    publications = (
        db.query(RegulatoryPublication)
        .filter(
            RegulatoryPublication.created_at >= lookback,
        )
        .order_by(RegulatoryPublication.created_at.desc())
        .limit(100)
        .all()
    )
    for publication in publications:
        if not publication_matches_organization(publication.source, organization):
            continue
        response = await run_gap_analysis(
            workflow_agent_request(
                trigger_type='publication',
                trigger_id=publication.id,
                org_id=org_id,
                policy_ids=[policy_id],
            )
        )
        responses.append(response)

    business_changes = (
        db.query(BusinessChange)
        .filter(
            BusinessChange.org_id == org_id,
            BusinessChange.triggers_review.is_(True),
            BusinessChange.flagged_at >= lookback,
        )
        .order_by(BusinessChange.flagged_at.desc())
        .limit(50)
        .all()
    )
    for change in business_changes:
        response = await run_gap_analysis(
            workflow_agent_request(
                trigger_type='business_change',
                trigger_id=change.id,
                org_id=org_id,
                policy_ids=[policy_id],
            )
        )
        responses.append(response)

    logger.info(
        'policy_re_evaluation_completed',
        policy_id=policy_id,
        org_id=org_id,
        publication_triggers=len(publications),
        business_change_triggers=len(business_changes),
        runs=len(responses),
    )
    return responses
