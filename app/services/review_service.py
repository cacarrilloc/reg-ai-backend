from datetime import datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import and_, desc, func, or_
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import Session, defer

from app.core.config import get_settings
from app.core.observability import langfuse_span, record_trace_score
from app.core.security import CurrentUser
from app.core.time import days_since, utc_now
from app.db.session import detach_instance, session_scope
from app.models.core import (
    AuditLog,
    BusinessChange,
    GapAnalysis,
    MarketingContent,
    Organization,
    Policy,
    PolicyRevision,
    RegulatoryPublication,
)
from app.schemas.gap_analyses import GapCitationResponse
from app.schemas.review import (
    AssignReviewerRequest,
    AssignReviewerResponse,
    AuditLogResponse,
    GapSummaryResponse,
    PendingRevisionResponse,
    PolicyAuditChainListResponse,
    PolicyAuditChainResponse,
    ReviewDashboardResponse,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
    ReviewHistoryResponse,
)
from app.services.marketing_gap_bridge import display_marketing_policy_name
from app.services.notification_service import (
    build_policy_upload_url,
    format_email_date,
    send_decision_notification,
    send_policy_review_request,
    send_reupload_reminder,
)
from app.services.organization_service import organization_notification_recipients
from app.services.storage_service import create_download_url, download_file, parse_storage_uri

logger = structlog.get_logger(__name__)

AUDIT_TRAIL_IMPACT_SCORES = ('HIGH', 'MEDIUM', 'CRITICAL')
AUDIT_TRAIL_TRIGGER_TYPES = ('business_change', 'marketing_content')
COMPLETED_REVISION_STATUSES = ('APPROVED', 'REJECTED', 'PARTIALLY_ACCEPTED')


def audit_trail_gap_filters():
    """Include business-change and marketing gaps plus any HIGH/MEDIUM gap analysis."""
    return or_(
        GapAnalysis.trigger_type.in_(AUDIT_TRAIL_TRIGGER_TYPES),
        GapAnalysis.impact_score.in_(AUDIT_TRAIL_IMPACT_SCORES),
    )


def gap_analysis_visible_in_audit_trail(gap: GapAnalysis) -> bool:
    """Mirror audit_trail_gap_filters() for tests and documentation."""
    impact = str(gap.impact_score or '').strip().upper()
    return gap.trigger_type in AUDIT_TRAIL_TRIGGER_TYPES or impact in AUDIT_TRAIL_IMPACT_SCORES


async def assign_revision_reviewer(
    revision_id: str,
    payload: AssignReviewerRequest,
    user: CurrentUser,
    db: Session,
) -> AssignReviewerResponse:
    """Persist the assigned reviewer and email a Policy Review Request for this revision."""
    revision: PolicyRevision | None = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.id == revision_id,
            PolicyRevision.org_id == user.org_id,
        )
        .one_or_none()
    )
    if revision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
    if revision.status != "PENDING":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Revision is {revision.status}; only pending reviews can be assigned",
        )

    escalate = bool(payload.escalate)
    current_reviewer_id = (revision.reviewer_id or "").strip()
    next_reviewer_id = payload.user_id.strip()
    if escalate:
        if not current_reviewer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Escalate only after a reviewer is already assigned",
            )
        if next_reviewer_id == current_reviewer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a different organization member to escalate to",
            )

    policy: Policy | None = (
        db.query(Policy)
        .filter(
            Policy.id == revision.policy_id,
            Policy.org_id == revision.org_id,
        )
        .one_or_none()
    )
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

    org: Organization | None = (
        db.query(Organization)
        .filter(Organization.auth_org_id == revision.org_id)
        .one_or_none()
    )
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    reviewer_email = (payload.email or "").strip() or None
    reviewer_name = (payload.full_name or "").strip() or (
        reviewer_email or payload.user_id
    )
    if not reviewer_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The selected reviewer must have an email address to receive the review request",
        )

    _apply_revision_reviewer_fields(
        revision,
        org,
        user_id=payload.user_id,
        reviewer_email=reviewer_email,
        reviewer_name=reviewer_name,
        stamp_escalated_at=escalate,
    )
    db.add(org)
    db.add(revision)

    settings = get_settings()
    dashboard_url = (
        f"{settings.frontend_url.rstrip('/')}/reviews"
        f"?revision_id={revision.id}"
    )
    policy_name = display_marketing_policy_name(policy.name)

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        if escalate:
            logger.warning(
                "assign_revision_reviewer_retry_without_escalated_at",
                error=str(exc),
                revision_id=revision_id,
            )
            revision = (
                db.query(PolicyRevision)
                .filter(
                    PolicyRevision.id == revision_id,
                    PolicyRevision.org_id == user.org_id,
                )
                .one_or_none()
            )
            org = (
                db.query(Organization)
                .filter(Organization.auth_org_id == user.org_id)
                .one_or_none()
            )
            if revision is None or org is None:
                logger.error("assign_revision_reviewer_commit_failed", error=str(exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Unable to save the assigned reviewer",
                ) from exc
            _apply_revision_reviewer_fields(
                revision,
                org,
                user_id=payload.user_id,
                reviewer_email=reviewer_email,
                reviewer_name=reviewer_name,
                stamp_escalated_at=False,
            )
            db.add(org)
            db.add(revision)
            try:
                db.commit()
            except Exception as retry_exc:
                db.rollback()
                logger.error("assign_revision_reviewer_commit_failed", error=str(retry_exc))
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Unable to save the assigned reviewer",
                ) from retry_exc
        else:
            logger.error("assign_revision_reviewer_commit_failed", error=str(exc))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to save the assigned reviewer",
            ) from exc

    try:
        escalated_at_value = revision.escalated_at
    except Exception:
        escalated_at_value = utc_now() if escalate else None

    # Release the request DB connection before Resend HTTP.
    db.close()

    email_result = await send_policy_review_request(
        to_email=reviewer_email,
        recipient_name=reviewer_name,
        policy_name=policy_name,
        dashboard_url=dashboard_url,
        escalated=escalate,
    )
    email_status = str(email_result.get("status") or "failed")
    default_subject = (
        f"Escalated to review redline: {policy_name}"
        if escalate
        else f"Assigned to review redline: {policy_name}"
    )
    email_subject = str(email_result.get("subject") or default_subject)
    email_error = str(email_result.get("error") or email_result.get("reason") or "").strip() or None

    logger.info(
        "revision_reviewer_assigned",
        revision_id=revision_id,
        policy_name=policy_name,
        reviewer_user_id=payload.user_id,
        reviewer_email=reviewer_email,
        escalated=escalate,
        email_status=email_status,
        email_kind="policy_review_request",
        email_subject=email_subject,
        email_error=email_error,
    )
    return AssignReviewerResponse(
        revision_id=revision_id,
        policy_name=policy_name,
        reviewer_user_id=payload.user_id,
        reviewer_email=reviewer_email,
        reviewer_name=reviewer_name,
        email_status=email_status,
        email_subject=email_subject,
        email_error=email_error,
        email_kind="policy_review_request",
        persisted=True,
        escalated=escalate,
        escalated_at=escalated_at_value,
    )


def _apply_revision_reviewer_fields(
    revision: PolicyRevision,
    org: Organization,
    *,
    user_id: str,
    reviewer_email: str,
    reviewer_name: str,
    stamp_escalated_at: bool,
) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    chain = dict(org.approval_chain or {})
    chain["assigned_reviewer_user_id"] = user_id
    chain["assigned_reviewer_email"] = reviewer_email
    chain["assigned_reviewer_name"] = reviewer_name
    org.approval_chain = chain
    flag_modified(org, "approval_chain")
    revision.reviewer_id = user_id
    revision.assigned_reviewer_name = reviewer_name
    revision.assigned_reviewer_email = reviewer_email
    if stamp_escalated_at and revision.escalated_at is None:
        revision.escalated_at = utc_now()


def _marketing_decision_outcome(raw_decision: str) -> tuple[str, str | None]:
    if raw_decision in {'APPROVE_REVIEW', 'APPROVED_ALL_CHANGES'}:
        return 'APPROVED', 'REVIEW'
    if raw_decision in {'REJECT_REVIEW', 'REJECTED'}:
        return 'REJECTED', None
    if raw_decision == 'PARTIALLY_ACCEPTED':
        return 'PARTIALLY_ACCEPTED', 'PARTIAL'
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail='Marketing reviews support Approve Review, Reject Review, and Partially Accepted only.',
    )


def _clear_marketing_gap_review_queue(db: Session, revision: PolicyRevision, raw_decision: str) -> None:
    if not revision.gap_analysis_id:
        return
    gap = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.id == revision.gap_analysis_id,
            GapAnalysis.org_id == revision.org_id,
        )
        .one_or_none()
    )
    if gap is None:
        return
    metadata = dict(gap.retrieval_metadata) if isinstance(gap.retrieval_metadata, dict) else {}
    metadata['redline_eligible'] = False
    metadata['cco_review_decision'] = raw_decision
    gap.retrieval_metadata = metadata
    from sqlalchemy.orm.attributes import flag_modified

    try:
        flag_modified(gap, 'retrieval_metadata')
    except Exception:
        gap.retrieval_metadata = metadata

    content_id = str(metadata.get('marketing_content_id') or gap.trigger_id or '').strip()
    if not content_id:
        return
    content = (
        db.query(MarketingContent)
        .filter(
            MarketingContent.id == content_id,
            MarketingContent.org_id == revision.org_id,
        )
        .one_or_none()
    )
    if content is None:
        return
    content.status = 'rejected' if raw_decision in {'REJECT_REVIEW', 'REJECTED'} else 'approved'
    content.updated_at = utc_now()


async def record_review_decision(
    revision_id: str,
    payload: ReviewDecisionRequest,
    user: CurrentUser,
    db: Session,
    background_tasks: BackgroundTasks | None = None,
) -> ReviewDecisionResponse:
    """Record a CCO review decision on a policy revision.

    Decision modes:
    - REJECTED: leave the queue; gap returns to ACTIVE.
    - APPROVED / APPROVED_WITH_EDITS: mark APPROVED → UPLOAD_PENDING until Policy Library upload.
    - APPROVED_ALL_CHANGES: accept all redline tracked changes, auto-upload a clean policy
      document, clear the gap from Regulation Gap Analysis, and ensure compliance_history.
    - Marketing APPROVE_REVIEW / REJECT_REVIEW / PARTIALLY_ACCEPTED: leave the Review Queue,
      write the Audit trail, and do not start a Policy Library upload.
    """
    revision: PolicyRevision | None = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.id == revision_id,
            PolicyRevision.org_id == user.org_id,
        )
        .one_or_none()
    )

    if not revision:
        logger.warning("revision_not_found", revision_id=revision_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")

    policy: Policy | None = (
        db.query(Policy)
        .filter(
            Policy.id == revision.policy_id,
            Policy.org_id == revision.org_id,
        )
        .one_or_none()
    )
    if not policy:
        logger.error("policy_not_found_for_revision", revision_id=revision_id, policy_id=revision.policy_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Policy not found")

    org: Organization | None = (
        db.query(Organization)
        .filter(
            Organization.auth_org_id == revision.org_id,
        )
        .one_or_none()
    )
    if not org:
        logger.error("org_not_found_for_revision", revision_id=revision_id, org_id=revision.org_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    if revision.status != "PENDING":
        logger.warning(
            "revision_already_reviewed",
            revision_id=revision_id,
            current_status=revision.status,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Revision is {revision.status}, cannot review again",
        )

    decision_at = utc_now()
    raw_decision = payload.decision.strip().upper()
    from app.services.marketing_redline_service import revision_is_marketing

    is_marketing = revision_is_marketing(db, revision)
    if is_marketing:
        approval_mode: str | None
        decision_status, approval_mode = _marketing_decision_outcome(raw_decision)
        if decision_status != 'REJECTED' and not revision.redline_file_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Revision has no redline file to approve",
            )
        revision.status = decision_status
        revision.reviewer_id = user.user_id
        revision.reviewed_at = decision_at
        revision.reuploaded_at = decision_at
        revision.reupload_reminded_at = None
        policy_version = None
        approved_file_url = revision.approved_file_url
        _clear_marketing_gap_review_queue(db, revision, raw_decision)
        _ensure_compliance_history_for_gap(db, revision)
        audit_log_id = str(uuid4())
        notify_email, notify_name = resolve_reupload_notify_contact(user, org)
        audit_log = AuditLog(
            id=audit_log_id,
            org_id=revision.org_id,
            event_type="REVISION_DECISION",
            actor_id=user.user_id,
            timestamp=decision_at,
            revision_id=revision_id,
            policy_id=revision.policy_id,
            gap_analysis_id=revision.gap_analysis_id,
            metadata_json={
                "decision": decision_status,
                "approval_mode": approval_mode,
                "raw_decision": raw_decision,
                "actor_id": user.user_id,
                "decided_at": decision_at.isoformat(),
                "reviewer_role": next(iter(user.roles), "UNKNOWN"),
                "requires_policy_upload": False,
                "policy_name": policy.name,
                "policy_version": policy_version,
                "reupload_notify_email": notify_email,
                "reupload_notify_name": notify_name,
                "marketing_review": True,
            },
            notes=payload.notes,
        )
        db.add(audit_log)
        db.commit()
        db.refresh(revision)
        reviewer_display_name = (
            current_user_display_name(user)
            or next(iter(user.roles), "Reviewer")
        )
        notification_kwargs = {
            "org_id": revision.org_id,
            "revision_id": revision.id,
            "policy_name": policy.name,
            "decision_status": decision_status,
            "reviewer_display_name": reviewer_display_name,
            "notes": payload.notes,
            "requires_policy_upload": False,
            "notify_email": notify_email,
            "notify_name": notify_name,
            "decision_at": decision_at,
            "approval_chain": dict(org.approval_chain or {}),
        }
        if background_tasks is not None:
            background_tasks.add_task(
                dispatch_review_decision_emails,
                **notification_kwargs,
            )
        else:
            await dispatch_review_decision_emails(**notification_kwargs)
        logger.info(
            "marketing_review_decision_recorded",
            revision_id=revision_id,
            decision=decision_status,
            approval_mode=approval_mode,
            actor_id=user.user_id,
            audit_log_id=audit_log_id,
        )
        record_trace_score(
            'cco_decision',
            value=1 if decision_status == 'APPROVED' else 0,
            comment=(payload.notes or decision_status)[:500],
        )
        return ReviewDecisionResponse(
            revision_id=revision_id,
            status=decision_status,
            audit_log_id=audit_log_id,
            policy_version=policy_version,
            approved_file_url=approved_file_url,
            policy_id=revision.policy_id,
            policy_name=display_marketing_policy_name(policy.name),
            requires_policy_upload=False,
            approval_mode=approval_mode,
        )

    if raw_decision == "APPROVED":
        approval_mode = "WITH_EDITS"
        decision_status = "APPROVED"
    elif raw_decision == "APPROVED_WITH_EDITS":
        approval_mode = "WITH_EDITS"
        decision_status = "APPROVED"
    elif raw_decision == "APPROVED_ALL_CHANGES":
        approval_mode = "ALL_CHANGES"
        decision_status = "APPROVED"
    else:
        approval_mode = None
        decision_status = "REJECTED"

    with langfuse_span(
        'review-decision',
        input_data={
            'revision_id': revision_id,
            'decision': raw_decision,
            'approval_mode': approval_mode,
        },
        metadata={
            'org_id': user.org_id,
            'policy_id': revision.policy_id,
            'gap_analysis_id': revision.gap_analysis_id,
        },
    ):
        if decision_status == "APPROVED" and not revision.redline_file_url:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Revision has no redline file to approve",
            )

        policy_version: int | None = None
        approved_file_url = revision.approved_file_url
        requires_policy_upload = False

        if decision_status == "APPROVED" and approval_mode == "ALL_CHANGES":
            from app.services.policy_service import increment_policy_version
            from app.services.tracked_redline_document import accept_all_tracked_changes

            try:
                redline_bytes = download_file(revision.redline_file_url)
                clean_bytes = accept_all_tracked_changes(redline_bytes)
                policy_version, approved_file_url = await increment_policy_version(
                    revision.policy_id,
                    revision.org_id,
                    revision.redline_file_url,
                    db,
                    approved_bytes=clean_bytes,
                )
            except HTTPException:
                raise
            except Exception as exc:
                logger.exception(
                    "approve_all_changes_failed",
                    revision_id=revision_id,
                    error_type=type(exc).__name__,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Unable to accept all redline changes and upload the clean policy.",
                ) from exc

            revision.status = decision_status
            revision.reviewer_id = user.user_id
            revision.reviewed_at = decision_at
            revision.approved_file_url = approved_file_url
            revision.reuploaded_at = decision_at
            revision.reupload_reminded_at = None
            _ensure_compliance_history_for_gap(db, revision)
        elif decision_status == "APPROVED":
            # Approve with edits: CCO will manually upload their edited file.
            revision.status = decision_status
            revision.reviewer_id = user.user_id
            revision.reviewed_at = decision_at
            revision.reuploaded_at = None
            revision.reupload_reminded_at = None
            requires_policy_upload = True
        else:
            revision.status = decision_status
            revision.reviewer_id = user.user_id
            revision.reviewed_at = decision_at

        audit_log_id = str(uuid4())
        notify_email, notify_name = resolve_reupload_notify_contact(user, org)
        audit_log = AuditLog(
            id=audit_log_id,
            org_id=revision.org_id,
            event_type="REVISION_DECISION",
            actor_id=user.user_id,
            timestamp=decision_at,
            revision_id=revision_id,
            policy_id=revision.policy_id,
            gap_analysis_id=revision.gap_analysis_id,
            metadata_json={
                "decision": decision_status,
                "approval_mode": approval_mode,
                "raw_decision": raw_decision,
                "actor_id": user.user_id,
                "decided_at": decision_at.isoformat(),
                "reviewer_role": next(iter(user.roles), "UNKNOWN"),
                "requires_policy_upload": requires_policy_upload,
                "policy_name": policy.name,
                "policy_version": policy_version,
                "reupload_notify_email": notify_email,
                "reupload_notify_name": notify_name,
            },
            notes=payload.notes,
        )

        db.add(audit_log)
        db.commit()
        db.refresh(revision)

        reviewer_display_name = (
            current_user_display_name(user)
            or next(iter(user.roles), "Reviewer")
        )
        notification_kwargs = {
            "org_id": revision.org_id,
            "revision_id": revision.id,
            "policy_name": policy.name,
            "decision_status": decision_status,
            "reviewer_display_name": reviewer_display_name,
            "notes": payload.notes,
            "requires_policy_upload": requires_policy_upload,
            "notify_email": notify_email,
            "notify_name": notify_name,
            "decision_at": decision_at,
            "approval_chain": dict(org.approval_chain or {}),
        }
        # Mark reminder attempt on the request path so the cron doesn't double-send
        # while background Clerk/Resend work is still in flight.
        if requires_policy_upload and notify_email:
            revision.reupload_reminded_at = decision_at
            db.add(revision)
            db.commit()

        # Clerk + Resend must not hold the request DB session — that starves page loads.
        if background_tasks is not None:
            background_tasks.add_task(
                dispatch_review_decision_emails,
                **notification_kwargs,
            )
        else:
            await dispatch_review_decision_emails(**notification_kwargs)

        logger.info(
            "review_decision_recorded",
            revision_id=revision_id,
            decision=decision_status,
            approval_mode=approval_mode,
            actor_id=user.user_id,
            audit_log_id=audit_log_id,
            requires_policy_upload=requires_policy_upload,
            policy_version=policy_version,
        )

        record_trace_score(
            'cco_decision',
            value=1 if decision_status == 'APPROVED' else 0,
            comment=(payload.notes or decision_status)[:500],
        )

        return ReviewDecisionResponse(
            revision_id=revision_id,
            status=decision_status,
            audit_log_id=audit_log_id,
            policy_version=policy_version,
            approved_file_url=approved_file_url,
            policy_id=revision.policy_id,
            policy_name=display_marketing_policy_name(policy.name),
            requires_policy_upload=requires_policy_upload,
            approval_mode=approval_mode,
        )


async def dispatch_review_decision_emails(
    *,
    org_id: str,
    revision_id: str,
    policy_name: str,
    decision_status: str,
    reviewer_display_name: str,
    notes: str | None,
    requires_policy_upload: bool,
    notify_email: str | None,
    notify_name: str,
    decision_at: datetime,
    approval_chain: dict[str, Any] | None,
) -> None:
    """Send org-wide decision emails (and approver-only reupload reminder) off-request."""
    settings = get_settings()
    organization = Organization(
        id=org_id,
        auth_org_id=org_id,
        name=org_id,
        approval_chain=approval_chain or {},
    )
    recipients = organization_notification_recipients(org_id, organization)
    dashboard_url = f"{settings.frontend_url.rstrip('/')}/reviews?revision_id={revision_id}"
    for recipient in recipients:
        await send_decision_notification(
            to_email=recipient.email,
            recipient_name=recipient.full_name,
            policy_name=policy_name,
            decision=decision_status,
            reviewer_name=reviewer_display_name,
            notes=notes,
            dashboard_url=dashboard_url,
        )

    if not (requires_policy_upload and notify_email):
        return

    upload_url = build_policy_upload_url(settings.frontend_url, policy_name)
    await send_reupload_reminder(
        to_email=notify_email,
        recipient_name=notify_name,
        policy_name=policy_name,
        hours_since_approval=0,
        upload_url=upload_url,
        is_immediate=True,
        approved_on=format_email_date(decision_at),
    )


def _ensure_compliance_history_for_gap(db: Session, revision: PolicyRevision) -> None:
    """Persist/refresh compliance_history when a gap is cleared from Gap Analysis."""
    if not revision.gap_analysis_id:
        return
    gap = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.id == revision.gap_analysis_id,
            GapAnalysis.org_id == revision.org_id,
        )
        .one_or_none()
    )
    if gap is None:
        return
    from app.services.compliance_history_service import record_history_from_gap_row

    record_history_from_gap_row(db, gap)


def _pending_regulator_similarity_fields(metadata: dict[str, object] | None) -> dict[str, object]:
    """Prefer redline-vs-regs cosine; then Internal Policy ↔ regs; then other stored scores."""
    from app.services.document_chunk_service import redline_regulator_similarity_fields
    from app.services.gap_analysis_service import (
        business_change_similarity_fields,
        regulator_similarity_fields,
    )
    from app.services.marketing_vector_gap import primary_marketing_cosine_similarity

    fields = redline_regulator_similarity_fields(metadata)
    if fields.get('redline_regulator_cosine_similarity') is not None:
        return fields
    gap_fields = regulator_similarity_fields(metadata)
    if gap_fields.get('regulator_cosine_similarity') is not None:
        return {
            'redline_regulator_cosine_similarity': gap_fields.get('regulator_cosine_similarity'),
            'redline_regulator_reference_policy_name': gap_fields.get(
                'regulator_reference_policy_name'
            ),
        }
    business_fields = business_change_similarity_fields(metadata)
    business_score = business_fields.get('business_change_cosine_similarity')
    if business_score is not None:
        return {
            'redline_regulator_cosine_similarity': business_score,
            'redline_regulator_reference_policy_name': None,
        }
    homepage = None
    if isinstance(metadata, dict):
        homepage = metadata.get('homepage_rule_similarity')
    marketing_score = primary_marketing_cosine_similarity({'cosine_similarity': homepage})
    if marketing_score is not None:
        return {
            'redline_regulator_cosine_similarity': marketing_score,
            'redline_regulator_reference_policy_name': None,
        }
    return {
        'redline_regulator_cosine_similarity': None,
        'redline_regulator_reference_policy_name': None,
    }


async def get_pending_revisions(
    org_id: str,
    db: Session,
    *,
    include_findings: bool = True,
) -> ReviewDashboardResponse:
    """Fetch all pending policy revisions for the org's CCO dashboard.

    When ``include_findings`` is false, omit per-finding RULE/GAP/FIX arrays so dashboard
    tables can load quickly. Review Queue should pass true (or lazy-load findings).
    """
    settings = get_settings()
    base_query = (
        db.query(PolicyRevision, GapAnalysis, Policy, RegulatoryPublication, BusinessChange, Organization)
        .join(GapAnalysis, GapAnalysis.id == PolicyRevision.gap_analysis_id)
        .join(Policy, Policy.id == PolicyRevision.policy_id)
        .outerjoin(RegulatoryPublication, RegulatoryPublication.id == GapAnalysis.publication_id)
        .outerjoin(BusinessChange, BusinessChange.id == GapAnalysis.business_change_id)
        .outerjoin(Organization, Organization.auth_org_id == PolicyRevision.org_id)
        .filter(
            PolicyRevision.status == "PENDING",
            PolicyRevision.org_id == org_id,
            PolicyRevision.review_requested_at.is_not(None),
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
        )
    )
    list_options = [
        *_publication_queue_defers(),
        defer(GapAnalysis.regulatory_citations),
    ]
    if not include_findings:
        list_options.extend((defer(GapAnalysis.rule), defer(GapAnalysis.fix)))

    def _load_pending() -> tuple[Any, Any, Any, list[Any]]:
        totals = (
            base_query.with_entities(
                func.count(PolicyRevision.id),
                func.count(PolicyRevision.id).filter(GapAnalysis.impact_score == "HIGH"),
                func.count(PolicyRevision.id).filter(
                    PolicyRevision.escalated_at.is_not(None)
                ),
            )
            .one()
        )
        rows = (
            _with_query_options(base_query, *list_options)
            .order_by(desc(PolicyRevision.created_at))
            .limit(50)
            .all()
        )
        return totals[0], totals[1], totals[2], rows

    total_pending, high_impact_count, escalated_count, pending_revisions = (
        _fetch_review_rows(db, _load_pending)
    )

    revision_responses: list[PendingRevisionResponse] = []

    from app.services.document_chunk_service import (
        redline_internal_similarity_fields,
    )
    from app.services.gap_analysis_service import build_gap_finding_summaries, count_gap_findings
    from app.services.marketing_gap_bridge import marketing_gap_api_fields

    for revision, gap_analysis, policy, publication, business_change, organization in pending_revisions:
        try:
            metadata = gap_analysis.retrieval_metadata or {}
            gaps = build_gap_finding_summaries(gap_analysis) if include_findings else []
            gap_count = (
                max(len(gaps), 1 if gap_analysis.impact_score in {'HIGH', 'MEDIUM'} else 0)
                if include_findings
                else count_gap_findings(gap_analysis)
            )
            days_pending = days_since(revision.review_requested_at or revision.created_at)
            marketing_fields = marketing_gap_api_fields(gap_analysis)
            marketing_rule_labels = list(marketing_fields.get('marketing_rule_labels') or [])
            if marketing_rule_labels:
                publication_label = '\n'.join(marketing_rule_labels)
            elif include_findings:
                publication_label = regulatory_publication_label(gap_analysis, publication)
            else:
                publication_label = _queue_publication_label(publication)
            revision_responses.append(
                PendingRevisionResponse(
                    revision_id=revision.id,
                    gap_analysis_id=revision.gap_analysis_id,
                    policy_id=revision.policy_id,
                    policy_name=display_marketing_policy_name(policy.name),
                    impact_score=gap_analysis.impact_score,
                    trigger_type=gap_analysis.trigger_type,
                    trigger_label=trigger_label_for_gap(gap_analysis, publication, business_change, policy),
                    regulatory_publication=publication_label,
                    business_change=(
                        business_change.description
                        if business_change is not None
                        else None
                    ),
                    change_date=(
                        business_change.change_date
                        or business_change.flagged_at
                        if business_change is not None
                        else (
                            publication.published_at
                            or publication.created_at
                            if publication is not None
                            else gap_analysis.created_at
                        )
                    ),
                    gap_summary=(
                        gap_analysis.gap_summary
                        if include_findings
                        else (gap_analysis.gap_summary or '')[:280]
                    ),
                    rule=gap_analysis.rule if include_findings else None,
                    fix=gap_analysis.fix if include_findings else None,
                    gap_count=gap_count,
                    gaps=gaps,
                    created_at=revision.created_at,
                    days_pending=days_pending,
                    redline_url=revision.redline_file_url,
                    redline_ready=bool(revision.redline_file_url),
                    reviewer_user_id=(revision.reviewer_id or "").strip() or None,
                    reviewer_name=revision_reviewer_name(revision, organization),
                    reviewer_email=revision_reviewer_email(revision, organization),
                    has_assigned_reviewer=revision_has_assigned_reviewer(revision),
                    sla_reminded_at=revision.sla_reminded_at,
                    escalated_at=revision.escalated_at,
                    escalation_days=organization.escalation_days if organization else None,
                    sla_reminder_days=settings.sla_reminder_days,
                    marketing_rule_labels=marketing_rule_labels,
                    **redline_internal_similarity_fields(metadata),
                    **_pending_regulator_similarity_fields(metadata),
                )
            )
        except Exception as exc:
            logger.exception(
                'pending_revision_row_skipped',
                revision_id=getattr(revision, 'id', None),
                gap_analysis_id=getattr(revision, 'gap_analysis_id', None),
                error_type=type(exc).__name__,
            )

    return ReviewDashboardResponse(
        pending_revisions=revision_responses,
        total_pending=total_pending,
        high_impact_count=high_impact_count,
        escalated_count=escalated_count,
    )


def _publication_queue_defers() -> tuple[Any, ...]:
    """Skip heavy / unmigrated publication columns on CCO queue reads.

    Production may lag alembic phase_43 (``feed_summary``). Selecting it 500s
    ``GET /reviews/pending`` the same way the Regulatory Feed did before
    ``_load_publication_rows`` started deferring the column.
    """
    return (
        defer(RegulatoryPublication.raw_text),
        defer(RegulatoryPublication.embedding_chunk_ids),
        defer(RegulatoryPublication.feed_summary),
    )


def _fetch_review_rows(db: Session, fetch: Any) -> Any:
    """Retry once after a missing-column abort so the dashboard can still load."""
    try:
        return fetch()
    except ProgrammingError:
        rollback = getattr(db, 'rollback', None)
        if callable(rollback):
            rollback()
        logger.warning('review_query_schema_column_missing')
        return fetch()


def _with_query_options(query: Any, *opts: Any) -> Any:
    """Apply SQLAlchemy loader options; no-op for test doubles without ``options``."""
    apply = getattr(query, 'options', None)
    if not callable(apply) or not opts:
        return query
    try:
        return apply(*opts)
    except Exception:
        return query


def _queue_publication_label(publication: RegulatoryPublication | None) -> str | None:
    """List-view publication label without parsing RULE/GAP/FIX finding bodies."""
    if publication is None:
        return None
    linked = ' · '.join(
        part for part in (publication.source, publication.title) if part
    )
    return linked or None


def get_gap_summary(
    gap_analysis_id: str,
    org_id: str,
    db: Session,
) -> GapSummaryResponse:
    """Fetch gap analysis summary for a revision review.

    Args:
        gap_analysis_id: Gap analysis ID
        db: Database session

    Returns:
        GapSummaryResponse with gap details and policy info
    """
    record = (
        db.query(GapAnalysis, Policy)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .filter(
            GapAnalysis.id == gap_analysis_id,
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
        )
        .one_or_none()
    )

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gap analysis not found")

    gap_analysis, policy = record

    return GapSummaryResponse(
        gap_analysis_id=gap_analysis_id,
        policy_id=gap_analysis.policy_id,
        policy_name=display_marketing_policy_name(policy.name),
        impact_score=gap_analysis.impact_score,
        gap_summary=gap_analysis.gap_summary,
        rule=gap_analysis.rule,
        fix=gap_analysis.fix,
        regulatory_citations=citation_responses(gap_analysis.regulatory_citations),
        trigger_type=gap_analysis.trigger_type,
    )


def get_redline_download_url(
    revision_id: str,
    org_id: str,
    db: Session,
) -> str:
    revision, _policy = get_revision_with_policy(revision_id, org_id, db)
    if not revision.redline_file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Revision does not have a redline file',
        )
    storage_path = revision.redline_file_url
    # Return the request connection before signed-URL HTTP to storage.
    db.close()
    return create_download_url(storage_path)


def get_redline_download_content(
    revision_id: str,
    org_id: str,
    db: Session,
) -> tuple[bytes, str, str]:
    """Download a redline DOCX through the backend using service-role storage access."""
    revision, policy = get_revision_with_policy(revision_id, org_id, db)
    return build_redline_download_content(revision, policy)


def build_redline_download_content(
    revision: PolicyRevision,
    policy: Policy,
) -> tuple[bytes, str, str]:
    if not revision.redline_file_url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Revision does not have a redline file',
        )

    content = download_file(revision.redline_file_url)
    stored_file = parse_storage_uri(revision.redline_file_url)
    filename = stored_file.path.rsplit('/', 1)[-1] or f'{policy.name}_redline.docx'
    return (
        content,
        filename,
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    )


async def get_redline_download_content_with_regeneration(
    revision_id: str,
    org_id: str,
) -> tuple[bytes, str, str]:
    """Download a redline, regenerating it when the stored file is missing from Supabase."""
    with session_scope(commit=False) as db:
        revision, policy = get_revision_with_policy(revision_id, org_id, db)
        needs_generation = not revision.redline_file_url
        from app.services.marketing_redline_service import revision_is_marketing

        is_marketing = revision_is_marketing(db, revision)

    if needs_generation:
        if is_marketing:
            from app.services.marketing_redline_service import ensure_marketing_redline_for_revision

            await ensure_marketing_redline_for_revision(revision_id, org_id)
        else:
            from app.services.redline_generation_service import ensure_pending_revision_redline

            await ensure_pending_revision_redline(revision_id, org_id)

    try:
        with session_scope(commit=False) as db:
            revision, policy = get_revision_with_policy(revision_id, org_id, db)
            revision = detach_instance(revision, db)
            policy = detach_instance(policy, db)
        return build_redline_download_content(revision, policy)
    except HTTPException as exc:
        with session_scope(commit=False) as db:
            revision, policy = get_revision_with_policy(revision_id, org_id, db)
            if revision.redline_file_url is None:
                raise
            logger.warning(
                'redline_download_missing_storage_object',
                revision_id=revision_id,
                org_id=org_id,
                redline_file_url=revision.redline_file_url,
                status_code=exc.status_code,
                detail=str(exc.detail),
            )
            revision.redline_file_url = None
            db.commit()
            from app.services.marketing_redline_service import revision_is_marketing as _revision_is_marketing

            is_marketing = _revision_is_marketing(db, revision)

        if is_marketing:
            from app.services.marketing_redline_service import ensure_marketing_redline_for_revision

            await ensure_marketing_redline_for_revision(revision_id, org_id)
        else:
            from app.services.redline_generation_service import (
                detach_redline_context,
                detach_revision,
                finalize_policy_redline_for_revision,
                load_redline_context,
            )

            with session_scope(commit=False) as db:
                revision, policy = get_revision_with_policy(revision_id, org_id, db)
                context = load_redline_context(revision.gap_analysis_id, org_id, db)
                revision = detach_revision(revision, db)
                context = detach_redline_context(context, db)
            await finalize_policy_redline_for_revision(revision, context, db=None)
        with session_scope(commit=False) as db:
            revision, policy = get_revision_with_policy(revision_id, org_id, db)
            revision = detach_instance(revision, db)
            policy = detach_instance(policy, db)
        return build_redline_download_content(revision, policy)


def get_pending_revision_for_gap_analysis(
    gap_analysis_id: str,
    org_id: str,
    db: Session,
) -> PolicyRevision | None:
    return (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'PENDING',
            PolicyRevision.review_requested_at.is_not(None),
        )
        .order_by(PolicyRevision.created_at.desc())
        .first()
    )


def get_revision_with_policy(
    revision_id: str,
    org_id: str,
    db: Session,
) -> tuple[PolicyRevision, Policy]:
    record = (
        db.query(PolicyRevision, Policy)
        .join(Policy, Policy.id == PolicyRevision.policy_id)
        .filter(
            PolicyRevision.id == revision_id,
            PolicyRevision.org_id == org_id,
            Policy.org_id == org_id,
        )
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Revision not found')
    return record


def get_policy_audit_chains(
    org_id: str,
    db: Session,
    policy_id: str | None = None,
    limit: int = 100,
) -> PolicyAuditChainListResponse:
    """Return trigger -> gap -> redline -> reviewer -> decision chains."""
    query = (
        db.query(GapAnalysis, Policy, RegulatoryPublication, BusinessChange, PolicyRevision, AuditLog)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .outerjoin(RegulatoryPublication, RegulatoryPublication.id == GapAnalysis.publication_id)
        .outerjoin(BusinessChange, BusinessChange.id == GapAnalysis.business_change_id)
        .outerjoin(
            PolicyRevision,
            and_(
                PolicyRevision.gap_analysis_id == GapAnalysis.id,
                PolicyRevision.org_id == org_id,
            ),
        )
        .outerjoin(
            AuditLog,
            and_(
                AuditLog.revision_id == PolicyRevision.id,
                AuditLog.org_id == org_id,
                AuditLog.event_type == "REVISION_DECISION",
            ),
        )
        .filter(
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
            audit_trail_gap_filters(),
        )
    )
    if policy_id:
        query = query.filter(Policy.id == policy_id)

    rows = _fetch_review_rows(
        db,
        lambda: (
            _with_query_options(query, *_publication_queue_defers())
            .order_by(Policy.name.asc(), desc(GapAnalysis.created_at))
            .limit(limit)
            .all()
        ),
    )

    chains = [
        audit_chain_response(
            gap_analysis=gap_analysis,
            policy=policy,
            publication=publication,
            business_change=business_change,
            revision=revision,
            audit_log=audit_log,
        )
        for gap_analysis, policy, publication, business_change, revision, audit_log in rows
    ]
    return PolicyAuditChainListResponse(chains=chains, total_chains=len(chains))


def get_review_history(
    org_id: str,
    db: Session,
    limit: int = 100,
) -> ReviewHistoryResponse:
    """Return completed approve/reject decisions for the audit history tab."""
    query = (
        db.query(GapAnalysis, Policy, RegulatoryPublication, BusinessChange, PolicyRevision, AuditLog)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .join(
            PolicyRevision,
            and_(
                PolicyRevision.gap_analysis_id == GapAnalysis.id,
                PolicyRevision.org_id == org_id,
            ),
        )
        .outerjoin(RegulatoryPublication, RegulatoryPublication.id == GapAnalysis.publication_id)
        .outerjoin(BusinessChange, BusinessChange.id == GapAnalysis.business_change_id)
        .outerjoin(
            AuditLog,
            and_(
                AuditLog.revision_id == PolicyRevision.id,
                AuditLog.org_id == org_id,
                AuditLog.event_type == 'REVISION_DECISION',
            ),
        )
        .filter(
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
            PolicyRevision.status.in_(COMPLETED_REVISION_STATUSES),
            audit_trail_gap_filters(),
        )
    )
    rows = _fetch_review_rows(
        db,
        lambda: (
            _with_query_options(query, *_publication_queue_defers())
            .order_by(desc(PolicyRevision.reviewed_at), desc(PolicyRevision.created_at))
            .limit(limit)
            .all()
        ),
    )
    decisions = [
        audit_chain_response(
            gap_analysis=gap_analysis,
            policy=policy,
            publication=publication,
            business_change=business_change,
            revision=revision,
            audit_log=audit_log,
        )
        for gap_analysis, policy, publication, business_change, revision, audit_log in rows
    ]
    return ReviewHistoryResponse(decisions=decisions, total_decisions=len(decisions))


def audit_chain_response(
    *,
    gap_analysis: GapAnalysis,
    policy: Policy,
    publication: RegulatoryPublication | None,
    business_change: BusinessChange | None,
    revision: PolicyRevision | None,
    audit_log: AuditLog | None,
) -> PolicyAuditChainResponse:
    decision = audit_decision(revision, audit_log)
    change_date = None
    if business_change is not None:
        change_date = business_change.change_date or business_change.flagged_at
    elif publication is not None:
        change_date = publication.published_at or publication.created_at
    created_at = gap_analysis.created_at or datetime.utcnow()
    metadata = (
        gap_analysis.retrieval_metadata
        if isinstance(gap_analysis.retrieval_metadata, dict)
        else {}
    )
    similarity_items = metadata.get('regulator_document_similarity') or []
    regulator_reference_policy_name = None
    if isinstance(similarity_items, list):
        for item in similarity_items:
            if not isinstance(item, dict):
                continue
            candidate = str(item.get('policy_name') or '').strip()
            if candidate:
                regulator_reference_policy_name = candidate
                break
    from app.services.gap_analysis_service import build_gap_finding_summaries

    return PolicyAuditChainResponse(
        policy_id=policy.id,
        policy_name=display_marketing_policy_name(policy.name),
        policy_version=policy.version,
        trigger_type=gap_analysis.trigger_type,
        trigger_label=trigger_label_for_gap(gap_analysis, publication, business_change, policy),
        trigger_id=gap_analysis.trigger_id,
        gap_analysis_id=gap_analysis.id,
        gap_summary=gap_analysis.gap_summary,
        impact_score=gap_analysis.impact_score,
        rule=gap_analysis.rule,
        gaps=build_gap_finding_summaries(gap_analysis),
        regulatory_citations=citation_responses(gap_analysis.regulatory_citations),
        regulator_reference_policy_name=regulator_reference_policy_name,
        created_at=created_at,
        change_date=change_date or created_at,
        redline_revision_id=revision.id if revision else None,
        redline_status=revision.status if revision else None,
        redline_created_at=revision.created_at if revision else None,
        reviewer_id=revision.reviewer_id if revision else None,
        decision=decision,
        decision_at=(audit_log.timestamp if audit_log else revision.reviewed_at if revision else None),
        decision_notes=audit_log.notes if audit_log else None,
        audit_log_id=audit_log.id if audit_log else None,
        approved_file_url=revision.approved_file_url if revision else None,
    )


def audit_decision(revision: PolicyRevision | None, audit_log: AuditLog | None) -> str | None:
    if audit_log is not None and isinstance(audit_log.metadata_json, dict):
        value = audit_log.metadata_json.get("decision")
        if isinstance(value, str) and value:
            return value
    if revision is not None and revision.status in COMPLETED_REVISION_STATUSES:
        return revision.status
    return None


def get_audit_logs(
    org_id: str,
    revision_id: str | None = None,
    limit: int = 100,
    db: Session | None = None,
) -> list[AuditLogResponse]:
    """Fetch audit logs for review decisions.

    Args:
        org_id: Organization ID
        revision_id: Optional filter by revision ID
        limit: Maximum number of logs to return
        db: Database session

    Returns:
        List of AuditLogResponse entries
    """
    if db is None:
        return []

    query = db.query(AuditLog).filter(
        AuditLog.org_id == org_id,
        AuditLog.event_type == "REVISION_DECISION",
    )

    if revision_id:
        query = query.filter(AuditLog.revision_id == revision_id)

    logs = query.order_by(desc(AuditLog.timestamp)).limit(limit).all()

    return [
        AuditLogResponse(
            id=log.id,
            event_type=log.event_type,
            actor_id=log.actor_id,
            timestamp=log.timestamp,
            revision_id=log.revision_id,
            policy_id=log.policy_id,
            gap_analysis_id=log.gap_analysis_id,
            notes=log.notes,
            metadata=log.metadata_json,
        )
        for log in logs
    ]


def regulatory_publication_label(
    gap_analysis: GapAnalysis,
    publication: RegulatoryPublication | None,
) -> str | None:
    """Display the same RULE values shown in Regulation Gap Analysis RULE/GAP/FIX."""
    from app.services.gap_analysis_service import build_gap_finding_summaries

    labels: list[str] = []
    for finding in build_gap_finding_summaries(gap_analysis):
        rule = str(finding.rule or '').strip()
        if rule and rule not in labels:
            labels.append(rule)

    if labels:
        return '; '.join(labels)

    top_level_rule = str(gap_analysis.rule or '').strip()
    if top_level_rule:
        return top_level_rule

    # Last resort when no RULE/GAP/FIX text exists yet.
    if publication is not None:
        linked = ' · '.join(
            part for part in (publication.source, publication.title) if part
        )
        if linked:
            return linked
    return None


def trigger_label_for_gap(
    gap_analysis: GapAnalysis,
    publication: RegulatoryPublication | None,
    business_change: BusinessChange | None,
    policy: Policy | None = None,
) -> str:
    if gap_analysis.trigger_type == "publication":
        if publication is not None and publication.title:
            return publication.title
        return gap_analysis.trigger_id

    if gap_analysis.trigger_type == 'policy_update':
        if policy is not None:
            return f'Policy upload: {policy.name} v{policy.version}'
        return f'Policy upload: {gap_analysis.trigger_id}'

    if gap_analysis.trigger_type == 'marketing_content':
        metadata = (
            gap_analysis.retrieval_metadata
            if isinstance(gap_analysis.retrieval_metadata, dict)
            else {}
        )
        url = str(metadata.get('source_url') or '').strip()
        title = (
            display_marketing_policy_name(policy.name)
            if policy is not None
            else 'Marketing homepage'
        )
        if url:
            return f'{title} · {url}'[:240]
        return title

    if business_change is not None:
        description = (business_change.description or "").strip()
        if description:
            return description[:240]
        return business_change.change_type.replace("_", " ").title()
    return gap_analysis.trigger_id


def _loaded_optional_str(instance: Any, attr: str) -> str:
    """Read a mapped string without lazy-loading an unmigrated column."""
    try:
        from sqlalchemy.orm.attributes import instance_state

        state = instance_state(instance)
    except Exception:
        return str(getattr(instance, attr, None) or "").strip()
    if attr in state.unloaded:
        return ""
    return str(getattr(instance, attr, None) or "").strip()


def revision_has_assigned_reviewer(revision: PolicyRevision) -> bool:
    """True only when this pending revision was explicitly assigned via Assign."""
    return bool((revision.reviewer_id or "").strip())


def has_assigned_reviewer(organization: Organization | None) -> bool:
    """Org-level sticky reviewer preference (used for escalation/default contact).

    Do not use this for Review Queue Assigned badges — those are per-revision.
    """
    if organization is None or not organization.approval_chain:
        return False
    chain = organization.approval_chain
    return bool(
        (chain.get("assigned_reviewer_user_id") or "").strip()
        or (chain.get("assigned_reviewer_email") or "").strip()
        or (chain.get("assigned_reviewer_name") or "").strip()
    )


def revision_reviewer_name(
    revision: PolicyRevision,
    organization: Organization | None,
) -> str | None:
    """Display name for the reviewer assigned to this revision, if any."""
    reviewer_id = (revision.reviewer_id or "").strip()
    if not reviewer_id:
        return None
    stored = _loaded_optional_str(revision, "assigned_reviewer_name")
    if stored:
        return stored
    if organization is None or not organization.approval_chain:
        return reviewer_id
    chain = organization.approval_chain
    chain_user_id = str(chain.get("assigned_reviewer_user_id") or "").strip()
    if chain_user_id and chain_user_id == reviewer_id:
        value = chain.get("assigned_reviewer_name")
        if value and str(value).strip():
            return str(value).strip()
    return reviewer_id


def revision_reviewer_email(
    revision: PolicyRevision,
    organization: Organization | None,
) -> str | None:
    """Email for the reviewer assigned to this revision, if known."""
    reviewer_id = (revision.reviewer_id or "").strip()
    if not reviewer_id:
        return None
    stored = _loaded_optional_str(revision, "assigned_reviewer_email")
    if stored:
        return stored
    if organization is None or not organization.approval_chain:
        return None
    chain = organization.approval_chain
    chain_user_id = str(chain.get("assigned_reviewer_user_id") or "").strip()
    if chain_user_id and chain_user_id == reviewer_id:
        value = chain.get("assigned_reviewer_email")
        if value and str(value).strip():
            return str(value).strip()
    return None


def reviewer_name(organization: Organization | None) -> str | None:
    if organization is None or not organization.approval_chain:
        return None
    value = organization.approval_chain.get("assigned_reviewer_name") or organization.approval_chain.get("cco_name")
    return str(value).strip() if value else None


def reviewer_email(organization: Organization | None) -> str | None:
    if organization is None or not organization.approval_chain:
        return None
    value = organization.approval_chain.get("assigned_reviewer_email") or organization.approval_chain.get("cco_email")
    return str(value).strip() if value else None


def current_user_email(user: CurrentUser | None) -> str | None:
    """Best-effort email from Clerk JWT claims for the acting reviewer."""
    if user is None or not user.claims:
        return None
    claims = user.claims
    for key in (
        "email",
        "email_address",
        "primary_email_address",
        "primary_email",
    ):
        value = claims.get(key)
        if isinstance(value, str) and "@" in value.strip():
            return value.strip()
    for path in (
        "primary_email_address.email_address",
    ):
        from app.core.security import claim_value

        value = claim_value(claims, path)
        if isinstance(value, str) and "@" in value.strip():
            return value.strip()
    return None


def current_user_display_name(user: CurrentUser | None) -> str | None:
    if user is None or not user.claims:
        return None
    claims = user.claims
    name = claims.get("name")
    if isinstance(name, str) and name.strip() and "@" not in name:
        return name.strip()
    first = str(claims.get("first_name") or claims.get("given_name") or "").strip()
    last = str(claims.get("last_name") or claims.get("family_name") or "").strip()
    combined = " ".join(part for part in (first, last) if part).strip()
    if combined and "@" not in combined:
        return combined
    return None


def resolve_reupload_notify_contact(
    user: CurrentUser | None,
    organization: Organization | None,
) -> tuple[str | None, str]:
    """Recipient for Approve-with-edits upload warning / daily reminders."""
    email = (
        current_user_email(user)
        or reviewer_email(organization)
        or organization_primary_email(organization)
    )
    name = (
        current_user_display_name(user)
        or reviewer_name(organization)
        or organization_primary_name(organization)
    )
    return email, name


def revision_reupload_notify_contact(
    db: Session,
    revision: PolicyRevision,
    organization: Organization | None,
) -> tuple[str | None, str]:
    """Load the notify contact stored on the decision audit log, with org fallbacks."""
    audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.revision_id == revision.id,
            AuditLog.event_type == "REVISION_DECISION",
        )
        .order_by(AuditLog.timestamp.desc())
        .first()
    )
    metadata = audit.metadata_json if audit and isinstance(audit.metadata_json, dict) else {}
    email = str(metadata.get("reupload_notify_email") or "").strip() or None
    name = str(metadata.get("reupload_notify_name") or "").strip() or None
    if not email:
        email = organization_primary_email(organization) or reviewer_email(organization)
    if not name:
        name = organization_primary_name(organization)
    return email, name


def organization_primary_email(organization: Organization | None) -> str | None:
    """Prefer the org CCO email, then the assigned reviewer (main operational contact)."""
    if organization is None or not organization.approval_chain:
        return None
    value = organization.approval_chain.get("cco_email") or organization.approval_chain.get(
        "assigned_reviewer_email"
    )
    return str(value).strip() if value else None


def organization_primary_name(organization: Organization | None) -> str:
    if organization is None or not organization.approval_chain:
        return "Compliance Team"
    value = organization.approval_chain.get("cco_name") or organization.approval_chain.get(
        "assigned_reviewer_name"
    )
    return str(value).strip() if value else "Compliance Team"


def mark_approved_revisions_reuploaded(
    db: Session,
    *,
    org_id: str,
    policy_name: str,
) -> int:
    """Mark approved revisions awaiting Policy Library upload as fulfilled for this policy name."""
    normalized = ' '.join((policy_name or '').split()).strip()
    if not normalized:
        return 0

    now = utc_now()
    revisions = (
        db.query(PolicyRevision)
        .join(Policy, Policy.id == PolicyRevision.policy_id)
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
            PolicyRevision.reuploaded_at.is_(None),
            PolicyRevision.reviewed_at.is_not(None),
            Policy.org_id == org_id,
            func.lower(Policy.name) == normalized.lower(),
        )
        .all()
    )
    for revision in revisions:
        revision.reuploaded_at = now
        db.add(revision)
        _ensure_compliance_history_for_gap(db, revision)
    if revisions:
        db.commit()
        logger.info(
            'approved_revisions_marked_reuploaded',
            org_id=org_id,
            policy_name=normalized,
            count=len(revisions),
        )
    return len(revisions)


def citation_responses(value: object) -> list[GapCitationResponse]:
    if not isinstance(value, list):
        return []

    citations: list[GapCitationResponse] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        reference = str(item.get("reference") or "").strip()
        if not reference:
            continue
        citations.append(
            GapCitationResponse(
                source=str(item.get("source") or "Trigger source").strip() or "Trigger source",
                reference=reference,
                quote=str(item.get("quote") or "").strip() or None,
                rationale=str(item.get("rationale") or "").strip() or None,
            )
        )
    return citations
