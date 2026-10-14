from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import structlog
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.security import CurrentUser
from app.models.core import (
    BusinessChange,
    BusinessChangeEventDelivery,
    GapAnalysis,
    Organization,
    PolicyRevision,
)
from app.schemas.policies import (
    BusinessChangeAnalysisDownloadResponse,
    BusinessChangeCreate,
    BusinessChangeListItem,
    BusinessChangeResponse,
)
from app.services.business_change_assessment import (
    BusinessChangeAssessment,
    assess_business_change,
)

logger = structlog.get_logger(__name__)


async def create_business_change(
    payload: BusinessChangeCreate,
    user: CurrentUser,
    db: Session | None,
    background_tasks: BackgroundTasks | None = None,
) -> BusinessChangeResponse:
    """Assess, persist, and fan out a firm-scoped business-change trigger."""
    settings = get_settings()
    regulatory_scope = organization_scope_for_user(db, user.org_id)
    assessment = await assess_business_change(
        change_type=payload.change_type,
        description=payload.description,
        change_date=payload.change_date,
        regulatory_scope=regulatory_scope,
        settings=settings,
        org_id=user.org_id,
    )
    change_id = str(uuid4())

    if db is None:
        return business_change_response(
            change_id=change_id,
            org_id=user.org_id,
            payload=payload,
            assessment=assessment,
            persisted=False,
            event_emitted=False,
        )

    change = BusinessChange(
        id=change_id,
        org_id=user.org_id,
        change_type=payload.change_type,
        description=payload.description,
        flagged_by=user.user_id,
        change_date=payload.change_date,
        triggers_review=assessment.triggers_review,
        assessment_rationale=assessment.rationale,
        assessment_model=assessment.model,
        assessment_mode=assessment.mode,
        event_summary=assessment.event_summary,
        assessed_at=datetime.now(UTC),
        analysis_status='PENDING',
    )
    try:
        db.add(change)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception('business_change_persistence_failed', error_type=type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to persist business change',
        ) from exc

    event_emitted = False
    inngest_delivered = False
    if settings.inngest_async_dispatch_enabled:
        enqueue_business_change_delivery(change, db)
        try:
            inngest_delivered = bool(await emit_pending_business_change_events(db, settings))
            event_emitted = inngest_delivered
        except Exception as exc:
            logger.exception('business_change_event_delivery_failed', error_type=type(exc).__name__)

    from app.services.workflow_dispatch_service import (
        queue_background_task,
        run_business_change_workflow_background,
    )

    # Always queue the local background workflow so analysis runs even when Inngest
    # accepted the event but no worker processed it (common in local dev).
    if queue_background_task(
        background_tasks,
        run_business_change_workflow_background,
        change.id,
        change.org_id,
    ):
        event_emitted = True

    if event_emitted and change.analysis_status == 'PENDING':
        change.analysis_status = 'GENERATING'
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception('business_change_generating_status_failed', change_id=change.id)

    if not event_emitted:
        logger.warning(
            'business_change_workflow_not_queued',
            change_id=change.id,
            org_id=change.org_id,
            reason='inngest_unavailable_and_no_background_tasks',
        )

    return business_change_response(
        change_id=change.id,
        org_id=change.org_id,
        payload=payload,
        assessment=assessment,
        persisted=True,
        event_emitted=event_emitted,
        analysis_status=change.analysis_status,
        analysis_generated_at=change.analysis_generated_at,
        flagged_at=change.flagged_at,
    )


def organization_scope_for_user(db: Session | None, org_id: str) -> dict[str, Any]:
    if db is None:
        return {}
    organization = db.query(Organization).filter(Organization.auth_org_id == org_id).one_or_none()
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Create the client organization before logging business changes',
        )
    return organization.regulatory_scope or {}


def enqueue_business_change_delivery(change: BusinessChange, db: Session) -> None:
    """Persist the event intent before attempting the external Inngest send."""
    delivery = BusinessChangeEventDelivery(
        id=str(uuid4()),
        business_change_id=change.id,
        org_id=change.org_id,
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
        db.commit()
    except IntegrityError:
        # A retry must not create another event for the same business change.
        db.rollback()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception('business_change_delivery_enqueue_failed', error_type=type(exc).__name__)
        raise


async def emit_pending_business_change_events(
    db: Session,
    settings: Settings,
    client: Any | None = None,
) -> int:
    """Send all outstanding business-change events and mark only successful deliveries sent."""
    if not settings.inngest_async_dispatch_enabled:
        return 0
    try:
        import inngest
    except ImportError:
        logger.warning('business_change_event_not_sent', reason='SDK is unavailable')
        return 0

    pending = cast(
        Sequence[tuple[BusinessChangeEventDelivery, BusinessChange]],
        (
            db.query(BusinessChangeEventDelivery, BusinessChange)
            .join(BusinessChange, BusinessChange.id == BusinessChangeEventDelivery.business_change_id)
            .filter(BusinessChangeEventDelivery.sent_at.is_(None))
            .order_by(BusinessChangeEventDelivery.created_at.asc())
            .limit(500)
            .all()
        ),
    )
    if not pending:
        return 0

    events = [
        inngest.Event(
            name='business_change.flagged',
            data={
                'business_change_id': change.id,
                'org_id': delivery.org_id,
                'change_type': change.change_type,
                'change_date': change.change_date.isoformat() if change.change_date else None,
            },
        )
        for delivery, change in pending
    ]
    if client is None:
        from app.core.inngest import create_inngest_client

        client = create_inngest_client(settings)
    event_ids = await client.send(events)
    for (delivery, _change), event_id in zip(pending, event_ids, strict=True):
        delivery.event_id = event_id
        delivery.sent_at = datetime.now(UTC)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception('business_change_delivery_mark_sent_failed', error_type=type(exc).__name__)
        raise
    return len(pending)


def business_change_response(
    *,
    change_id: str,
    org_id: str,
    payload: BusinessChangeCreate,
    assessment: BusinessChangeAssessment,
    persisted: bool,
    event_emitted: bool,
    analysis_status: str = 'PENDING',
    analysis_generated_at: datetime | None = None,
    flagged_at: datetime | None = None,
) -> BusinessChangeResponse:
    return BusinessChangeResponse(
        id=change_id,
        org_id=org_id,
        change_type=payload.change_type,
        description=payload.description,
        change_date=payload.change_date,
        triggers_review=assessment.triggers_review,
        assessment_rationale=assessment.rationale,
        event_summary=assessment.event_summary,
        assessment_mode=assessment.mode,
        assessment_model=assessment.model,
        persisted=persisted,
        event_emitted=event_emitted,
        flagged_at=flagged_at,
        analysis_status=analysis_status,
        analysis_generated_at=analysis_generated_at,
    )


def get_business_change_for_org(
    db: Session | None,
    org_id: str,
    change_id: str,
) -> BusinessChangeListItem:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Business change lookup requires a configured database',
        )
    change = (
        db.query(BusinessChange)
        .filter(BusinessChange.id == change_id, BusinessChange.org_id == org_id)
        .one_or_none()
    )
    if change is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Business change not found',
        )
    gap_fields = _gap_review_fields_for_changes(db, org_id, [change.id])
    return business_change_list_item_from_record(change, gap_fields=gap_fields.get(change.id))


def _gap_review_fields_for_changes(
    db: Session,
    org_id: str,
    change_ids: list[str],
) -> dict[str, tuple[str, str, bool]]:
    """Map each business change to its latest Gap Analysis id and review status."""
    if not change_ids:
        return {}
    from app.services.business_change_policy_workflow_service import (
        gap_analysis_is_redline_eligible,
    )
    from app.services.gap_analysis_service import (
        gap_workflow_status,
        load_latest_revisions_for_gaps,
    )

    gaps = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.org_id == org_id,
            GapAnalysis.business_change_id.in_(change_ids),
        )
        .order_by(GapAnalysis.created_at.desc())
        .all()
    )
    latest: dict[str, GapAnalysis] = {}
    for gap in gaps:
        change_id = str(gap.business_change_id or '')
        if change_id and change_id not in latest:
            latest[change_id] = gap
    revisions = load_latest_revisions_for_gaps(db, org_id, [gap.id for gap in latest.values()])
    return {
        change_id: (
            gap.id,
            gap_workflow_status(revisions.get(gap.id)),
            gap_analysis_is_redline_eligible(
                gap.retrieval_metadata if isinstance(gap.retrieval_metadata, dict) else None,
                trigger_type=str(gap.trigger_type or ''),
            ),
        )
        for change_id, gap in latest.items()
    }


def business_change_list_item_from_record(
    change: BusinessChange,
    *,
    gap_fields: tuple[str, str, bool] | None = None,
) -> BusinessChangeListItem:
    if gap_fields is not None:
        gap_analysis_id, gap_status, gap_redline_eligible = gap_fields
    else:
        gap_analysis_id, gap_status, gap_redline_eligible = None, None, False
    return BusinessChangeListItem(
        id=change.id,
        org_id=change.org_id,
        change_type=change.change_type,
        description=change.description,
        change_date=change.change_date,
        triggers_review=change.triggers_review,
        assessment_rationale=change.assessment_rationale,
        event_summary=change.event_summary or '',
        assessment_mode=change.assessment_mode,
        flagged_at=change.flagged_at,
        analysis_status=change.analysis_status,
        analysis_generated_at=change.analysis_generated_at,
        gap_analysis_id=gap_analysis_id,
        gap_status=gap_status,
        gap_redline_eligible=gap_redline_eligible,
    )


def delete_business_change_for_org(
    db: Session | None,
    org_id: str,
    change_id: str,
) -> bool:
    """Remove a business-change history row and its related gap analyses."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Business change delete requires a configured database',
        )
    change = (
        db.query(BusinessChange)
        .filter(BusinessChange.id == change_id, BusinessChange.org_id == org_id)
        .one_or_none()
    )
    if change is None:
        return False

    gaps = (
        db.query(GapAnalysis)
        .filter(GapAnalysis.org_id == org_id, GapAnalysis.business_change_id == change_id)
        .all()
    )
    gap_ids = [gap.id for gap in gaps]
    revision_count = 0
    if gap_ids:
        revision_count = (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.org_id == org_id,
                PolicyRevision.gap_analysis_id.in_(gap_ids),
            )
            .delete(synchronize_session=False)
        )
        for gap in gaps:
            db.delete(gap)

    db.delete(change)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'business_change_delete_failed',
            change_id=change_id,
            org_id=org_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail='Unable to delete business change',
        ) from exc

    logger.info(
        'business_change_deleted',
        org_id=org_id,
        change_id=change_id,
        cascaded_gaps=len(gap_ids),
        cascaded_revisions=revision_count,
    )
    return True


def list_business_changes_for_org(
    db: Session | None,
    org_id: str,
    limit: int = 50,
) -> list[BusinessChangeListItem]:
    if db is None:
        return []
    records = (
        db.query(BusinessChange)
        .filter(BusinessChange.org_id == org_id)
        .order_by(BusinessChange.flagged_at.desc())
        .limit(limit)
        .all()
    )
    gap_fields = _gap_review_fields_for_changes(db, org_id, [change.id for change in records])
    return [
        business_change_list_item_from_record(change, gap_fields=gap_fields.get(change.id))
        for change in records
    ]


def trigger_business_change_analysis_generation(
    db: Session | None,
    org_id: str,
    change_id: str,
    background_tasks: object | None,
) -> dict[str, object]:
    """Queue AI regulatory analysis generation for one business change."""
    from app.services.business_change_analysis_service import (
        ANALYSIS_STATUS_GENERATING,
        mark_business_change_analysis_status,
    )
    from app.services.workflow_dispatch_service import (
        queue_background_task,
        run_business_change_workflow_background,
    )

    get_business_change_for_org(db, org_id, change_id)
    mark_business_change_analysis_status(change_id, org_id, ANALYSIS_STATUS_GENERATING)

    if db is not None:
        queued = queue_background_task(
            background_tasks,
            run_business_change_workflow_background,
            change_id,
            org_id,
        )
        if not queued:
            logger.warning(
                'business_change_analysis_retry_not_queued',
                change_id=change_id,
                org_id=org_id,
            )

    return {
        'change_id': change_id,
        'analysis_status': ANALYSIS_STATUS_GENERATING,
        'message': 'Business change regulatory analysis generation queued.',
    }


def get_business_change_analysis_download(
    db: Session | None,
    org_id: str,
    change_id: str,
) -> BusinessChangeAnalysisDownloadResponse:
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Business change analysis download requires a configured database',
        )
    change = (
        db.query(BusinessChange)
        .filter(BusinessChange.id == change_id, BusinessChange.org_id == org_id)
        .one_or_none()
    )
    if change is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Business change not found',
        )
    from app.services.business_change_analysis_service import business_change_analysis_download

    payload = business_change_analysis_download(change)
    return BusinessChangeAnalysisDownloadResponse(**payload)
