"""Phase 7 CCO Review Workflow - Inngest job handlers for SLA and escalation."""

from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, func, or_

from app.core.config import get_settings
from app.core.time import days_since, to_utc, utc_now
from app.db.session import SessionLocal
from app.models.core import Organization, Policy, PolicyRevision
from app.services.notification_service import (
    build_policy_upload_url,
    format_email_date,
    send_escalation_notification,
    send_reupload_reminder,
    send_sla_reminder,
)
from app.services.review_service import revision_reupload_notify_contact

logger = structlog.get_logger(__name__)


def revision_queue_anchor(revision: PolicyRevision) -> datetime:
    """Clock for SLA/escalation: when the item entered the Review Queue."""
    return revision.review_requested_at or revision.created_at


def _approval_chain(org: Organization) -> dict[str, Any]:
    chain = org.approval_chain
    return chain if isinstance(chain, dict) else {}


def _sla_reminder_recipient(org: Organization) -> tuple[str | None, str]:
    """Prefer assigned reviewer; fall back to CCO so unassigned items still get nudged."""
    chain = _approval_chain(org)
    if chain.get("assigned_reviewer_email"):
        email = chain.get("assigned_reviewer_email")
        name = chain.get("assigned_reviewer_name") or "Reviewer"
    else:
        email = chain.get("cco_email")
        name = chain.get("cco_name") or "Chief Compliance Officer"
    return (str(email).strip() if email else None, str(name))


async def check_sla_reminders() -> dict[str, Any]:
    """Inngest job: Send SLA reminders for pending revisions reaching threshold.

    Triggered: Daily at configurable time (default: 9 AM)
    Logic:
        - Find PENDING Review Queue items not reminded in past 24h
        - Age from review_requested_at (when the redline entered the queue)
        - Remind assigned reviewer (or CCO fallback) after SLA_REMINDER_DAYS
        - Mark sla_reminded_at timestamp
    """
    settings = get_settings()
    sla_reminder_days = settings.sla_reminder_days
    sla_deadline = utc_now() - timedelta(days=sla_reminder_days)
    reminder_threshold = utc_now() - timedelta(days=1)
    queue_anchor = func.coalesce(PolicyRevision.review_requested_at, PolicyRevision.created_at)

    session_factory = SessionLocal
    if session_factory is None:
        logger.error("review_sla_session_unavailable")
        return {"status": "failed", "error": "Database session is not configured"}

    db = session_factory()
    try:
        revisions_to_remind = (
            db.query(PolicyRevision, Organization, Policy)
            .join(Organization, PolicyRevision.org_id == Organization.auth_org_id)
            .join(Policy, PolicyRevision.policy_id == Policy.id)
            .filter(
                and_(
                    PolicyRevision.status == "PENDING",
                    PolicyRevision.review_requested_at.is_not(None),
                    queue_anchor < sla_deadline,
                    or_(
                        PolicyRevision.sla_reminded_at.is_(None),
                        PolicyRevision.sla_reminded_at < reminder_threshold,
                    ),
                )
            )
            .all()
        )

        reminded_count = 0
        for revision, org, policy in revisions_to_remind:
            reviewer_email, reviewer_name = _sla_reminder_recipient(org)

            if not reviewer_email:
                logger.warning(
                    "sla_reminder_no_reviewer_email",
                    org_id=org.auth_org_id,
                    revision_id=revision.id,
                )
                continue

            days_pending = days_since(revision_queue_anchor(revision))
            dashboard_url = f"{settings.frontend_url.rstrip('/')}/reviews?revision_id={revision.id}"

            result = await send_sla_reminder(
                to_email=reviewer_email,
                recipient_name=reviewer_name,
                policy_name=policy.name,
                days_pending=days_pending,
                dashboard_url=dashboard_url,
            )

            if result["status"] == "sent":
                revision.sla_reminded_at = utc_now()
                db.add(revision)
                reminded_count += 1

        db.commit()

        logger.info(
            "sla_reminders_sent",
            total_reminded=reminded_count,
            total_checked=len(revisions_to_remind),
        )

        return {
            "status": "completed",
            "reminded_count": reminded_count,
            "checked_count": len(revisions_to_remind),
        }

    except Exception as e:
        logger.error("sla_reminder_job_failed", error=str(e))
        db.rollback()
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


async def check_escalations() -> dict[str, Any]:
    """Inngest job: Escalate pending revisions to CCO if org escalation SLA exceeded.

    Triggered: Daily at configurable time (default: 10 AM)
    Logic:
        - Find PENDING Review Queue items not yet escalated
        - Age from review_requested_at vs organization.escalation_days
        - Notify CCO (compliance owner) once
        - Mark escalated_at timestamp
    """
    session_factory = SessionLocal
    if session_factory is None:
        logger.error("review_escalation_session_unavailable")
        return {"status": "failed", "error": "Database session is not configured"}

    db = session_factory()
    try:
        revisions_to_check = (
            db.query(PolicyRevision, Organization, Policy)
            .join(Organization, PolicyRevision.org_id == Organization.auth_org_id)
            .join(Policy, PolicyRevision.policy_id == Policy.id)
            .filter(
                and_(
                    PolicyRevision.status == "PENDING",
                    PolicyRevision.review_requested_at.is_not(None),
                    PolicyRevision.escalated_at.is_(None),
                )
            )
            .all()
        )

        escalated_count = 0
        settings = get_settings()
        for revision, org, policy in revisions_to_check:
            escalation_days = org.escalation_days or 7
            escalation_threshold = utc_now() - timedelta(days=escalation_days)

            if to_utc(revision_queue_anchor(revision)) >= escalation_threshold:
                continue

            chain = _approval_chain(org)
            cco_email = chain.get("cco_email")
            cco_name = chain.get("cco_name") or "Chief Compliance Officer"
            assigned_reviewer = chain.get("assigned_reviewer_name") or "Unassigned"

            if not cco_email:
                logger.warning(
                    "escalation_no_cco_email",
                    org_id=org.auth_org_id,
                    revision_id=revision.id,
                )
                continue

            dashboard_url = f"{settings.frontend_url.rstrip('/')}/reviews?revision_id={revision.id}"

            result = await send_escalation_notification(
                to_email=str(cco_email).strip(),
                recipient_name=str(cco_name),
                policy_name=policy.name,
                escalation_days=escalation_days,
                assigned_reviewer=str(assigned_reviewer),
                dashboard_url=dashboard_url,
            )

            if result["status"] == "sent":
                revision.escalated_at = utc_now()
                db.add(revision)
                escalated_count += 1

        db.commit()

        logger.info(
            "escalation_notifications_sent",
            escalated_count=escalated_count,
            total_checked=len(revisions_to_check),
        )

        return {
            "status": "completed",
            "escalated_count": escalated_count,
            "checked_count": len(revisions_to_check),
        }

    except Exception as e:
        logger.error("escalation_job_failed", error=str(e))
        db.rollback()
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


async def check_approved_reupload_reminders() -> dict[str, Any]:
    """Remind the acting reviewer that Approve-with-edits still needs Policy Library upload.

    Schedule: hourly. Immediate warning is sent on decision. This job then sends a
    follow-up once every 24 hours until the matching Internal Policy is uploaded
    (reuploaded_at set). Falls back to a grace window when the immediate email failed.
    """
    settings = get_settings()
    grace_hours = max(1, int(settings.reupload_reminder_grace_hours))
    grace_deadline = utc_now() - timedelta(hours=grace_hours)
    daily_threshold = utc_now() - timedelta(days=1)

    session_factory = SessionLocal
    if session_factory is None:
        logger.error("reupload_reminder_session_unavailable")
        return {"status": "failed", "error": "Database session is not configured"}

    db = session_factory()
    try:
        revisions_to_remind = (
            db.query(PolicyRevision, Organization, Policy)
            .join(Organization, PolicyRevision.org_id == Organization.auth_org_id)
            .join(Policy, PolicyRevision.policy_id == Policy.id)
            .filter(
                and_(
                    PolicyRevision.status == "APPROVED",
                    PolicyRevision.reviewed_at.is_not(None),
                    PolicyRevision.reuploaded_at.is_(None),
                    or_(
                        # Immediate email already sent → remind daily.
                        and_(
                            PolicyRevision.reupload_reminded_at.is_not(None),
                            PolicyRevision.reupload_reminded_at < daily_threshold,
                        ),
                        # Immediate email never sent → first reminder after grace.
                        and_(
                            PolicyRevision.reupload_reminded_at.is_(None),
                            PolicyRevision.reviewed_at < grace_deadline,
                        ),
                    ),
                )
            )
            .all()
        )

        reminded_count = 0
        for revision, org, policy in revisions_to_remind:
            to_email, recipient_name = revision_reupload_notify_contact(db, revision, org)
            if not to_email:
                logger.warning(
                    "reupload_reminder_no_recipient_email",
                    org_id=org.auth_org_id,
                    revision_id=revision.id,
                )
                continue

            reviewed_at = to_utc(revision.reviewed_at) if revision.reviewed_at else None
            hours_since = (
                max(1, int((utc_now() - reviewed_at).total_seconds() // 3600))
                if reviewed_at is not None
                else grace_hours
            )
            upload_url = build_policy_upload_url(settings.frontend_url, policy.name)
            result = await send_reupload_reminder(
                to_email=to_email,
                recipient_name=recipient_name,
                policy_name=policy.name,
                hours_since_approval=hours_since,
                upload_url=upload_url,
                is_immediate=False,
                approved_on=format_email_date(reviewed_at),
            )
            if result.get("status") == "sent":
                revision.reupload_reminded_at = utc_now()
                db.add(revision)
                reminded_count += 1

        db.commit()
        logger.info(
            "reupload_reminders_sent",
            total_reminded=reminded_count,
            total_checked=len(revisions_to_remind),
        )
        return {
            "status": "completed",
            "reminded_count": reminded_count,
            "checked_count": len(revisions_to_remind),
        }
    except Exception as e:
        logger.error("reupload_reminder_job_failed", error=str(e))
        db.rollback()
        return {"status": "failed", "error": str(e)}
    finally:
        db.close()


async def generate_material_gap_redlines() -> dict[str, Any]:
    """Generate revision DOCX files for HIGH/MEDIUM gaps on weekdays (cron: 3x daily Mon-Fri)."""
    from datetime import UTC, datetime

    if datetime.now(UTC).weekday() >= 5:
        return {"status": "skipped", "reason": "weekend"}

    session_factory = SessionLocal
    if session_factory is None:
        logger.error("material_gap_redline_session_unavailable")
        return {"status": "failed", "error": "Database session is not configured"}

    from app.services.redline_generation_service import generate_material_gap_redlines_for_org

    db = session_factory()
    try:
        org_ids = [org_id for (org_id,) in db.query(Organization.auth_org_id).distinct().all()]
        generated = 0
        for org_id in org_ids:
            generated += await generate_material_gap_redlines_for_org(org_id, db)
        logger.info("material_gap_redline_job_completed", generated=generated, org_count=len(org_ids))
        return {"status": "completed", "generated": generated, "org_count": len(org_ids)}
    except Exception as exc:
        logger.error("material_gap_redline_job_failed", error=str(exc))
        db.rollback()
        return {"status": "failed", "error": str(exc)}
    finally:
        db.close()
