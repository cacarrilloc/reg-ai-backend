"""Email notification helpers for review workflow."""

# ruff: noqa: E501

from html import escape
from typing import Any

import structlog

from app.core.config import get_settings

try:
    import resend as _resend_sdk
except ImportError:  # pragma: no cover - exercised when the package is missing
    _resend_sdk = None

# Kept for tests that patch this name; prefer send_resend_email() for sending.
resend_sdk: Any | None = _resend_sdk
Resend: Any | None = _resend_sdk

logger = structlog.get_logger(__name__)


def html_text(value: object) -> str:
    return escape(str(value), quote=True)


def send_resend_email(*, to_email: str, subject: str, html: str) -> dict[str, Any]:
    """Send one email via the Resend Python SDK (v2 module API)."""
    settings = get_settings()
    if not settings.resend_api_key:
        logger.warning("resend_api_key_not_configured")
        return {"status": "skipped", "reason": "Resend API key not configured"}

    sdk = resend_sdk
    if sdk is None:
        logger.warning("resend_not_installed")
        return {"status": "skipped", "reason": "Resend package not installed"}

    try:
        sdk.api_key = settings.resend_api_key
        response = sdk.Emails.send(
            {
                "from": settings.resend_from_email,
                "to": [to_email] if isinstance(to_email, str) else to_email,
                "subject": subject,
                "html": html,
            }
        )
        message_id = None
        if isinstance(response, dict):
            message_id = response.get("id")
        else:
            message_id = getattr(response, "id", None)
        return {"status": "sent", "to": to_email, "message_id": message_id}
    except Exception as exc:
        error_text = str(exc)
        # Resend sandbox sender only delivers to the account owner's email.
        if "onboarding@resend.dev" in (settings.resend_from_email or "") and (
            "only send testing emails" in error_text.lower()
            or "verify a domain" in error_text.lower()
        ):
            error_text = (
                f"{error_text} "
                "Tip: RESEND_FROM_EMAIL is onboarding@resend.dev (sandbox). "
                "It can only deliver to your Resend account email. "
                "To email other reviewers, use "
                "Reg-AI <notifications@alerts.ipxenterprises.com>."
            )
        logger.error("failed_to_send_resend_email", error=error_text, subject=subject)
        return {"status": "failed", "error": error_text}


async def send_revision_notification(
    to_email: str,
    recipient_name: str,
    regulation: str,
    impact_score: str,
    policy_name: str,
    dashboard_url: str,
    *,
    gap_summary: str | None = None,
) -> dict:
    """Send Resend email when Request Review is submitted.

    Shows the affected Regulation (same content as the Reviews Pending Approval table),
    not the full gap summary. ``gap_summary`` is ignored and kept only for call-site compat.
    """
    del gap_summary
    safe_recipient_name = html_text(recipient_name)
    safe_policy_name = html_text(policy_name)
    safe_impact_score = html_text(impact_score)
    safe_regulation = html_text((regulation or "").strip() or "—")
    safe_dashboard_url = html_text(dashboard_url)
    badge_class = impact_score.lower() if impact_score else "medium"

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #0f172a; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: #f1f5f9; padding: 20px; }}
        .footer {{ background-color: #1e293b; color: white; padding: 15px; text-align: center; border-radius: 0 0 8px 8px; }}
        .badge {{ display: inline-block; padding: 4px 12px; border-radius: 4px; font-weight: bold; }}
        .badge-high {{ background-color: #ef4444; color: white; }}
        .badge-medium {{ background-color: #f59e0b; color: white; }}
        .badge-low {{ background-color: #10b981; color: white; }}
        .badge-info {{ background-color: #64748b; color: white; }}
        .button {{ background-color: #3b82f6; color: white; padding: 12px 24px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Policy Review Required</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p>A new policy revision is pending your review:</p>
            <h2>{safe_policy_name}</h2>
            <p><strong>Impact Score:</strong> <span class="badge badge-{badge_class}">{safe_impact_score}</span></p>
            <p><strong>Regulation:</strong></p>
            <p>{safe_regulation}</p>
            <p><a href="{safe_dashboard_url}" class="button">Review in Dashboard</a></p>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(
        to_email=to_email,
        subject=f"Policy Review Required: {policy_name}",
        html=email_html,
    )
    if result.get("status") == "sent":
        logger.info(
            "revision_notification_sent",
            to_email=to_email,
            policy_name=policy_name,
            response_id=result.get("message_id"),
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_revision_notification", error=result.get("error"))
    return result


async def send_policy_review_request(
    to_email: str,
    recipient_name: str,
    policy_name: str,
    dashboard_url: str,
    *,
    escalated: bool = False,
) -> dict:
    """Email sent ONLY by the Review Queue Assign / Escalate buttons."""
    safe_recipient_name = html_text((recipient_name or "").strip() or "Colleague")
    safe_policy_name = html_text(policy_name)
    safe_dashboard_url = html_text(dashboard_url)
    action = "Escalated" if escalated else "Assigned"
    subject = f"{action} to review redline: {policy_name}"

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; color: #0f172a; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #0f172a; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .header h1 {{ margin: 0; font-size: 22px; font-weight: 600; }}
        .content {{ background-color: #f1f5f9; padding: 24px 20px; line-height: 1.55; font-size: 15px; }}
        .content p {{ margin: 0 0 14px; }}
        .content ul {{ margin: 0 0 14px; padding-left: 1.2rem; }}
        .content li {{ margin-bottom: 8px; }}
        .footer {{ background-color: #1e293b; color: #e2e8f0; padding: 14px 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 13px; }}
        .button {{ background-color: #2563eb; color: white !important; padding: 12px 22px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 8px; font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{action} to review redline</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p>
                You have been {action.lower()} to review the <strong>{safe_policy_name}</strong> redline.
            </p>
            <p>Submit your review with one of these options:</p>
            <ul>
                <li>
                    <strong>Accept all suggested changes</strong> — accept every Reg-AI edit as
                    proposed; the clean policy is published automatically.
                </li>
                <li>
                    <strong>Reject all suggested changes</strong> — decline the redline; no policy
                    update is published.
                </li>
                <li>
                    <strong>Accept with edits</strong> — keep the redline direction but revise the
                    document offline. You must then upload the final edited file to the Policy
                    Library so Reg-AI can update <strong>{safe_policy_name}</strong> and clear the gap.
                </li>
            </ul>
            <p><a href="{safe_dashboard_url}" class="button">Review in Dashboard</a></p>
        </div>
        <div class="footer">
            <p style="margin:0;">&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(
        to_email=to_email,
        subject=subject,
        html=email_html,
    )
    result = {**result, "email_kind": "policy_review_request", "subject": subject}
    if result.get("status") == "sent":
        logger.info(
            "policy_review_request_sent",
            to_email=to_email,
            policy_name=policy_name,
            subject=subject,
            response_id=result.get("message_id"),
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_policy_review_request", error=result.get("error"), subject=subject)
    return result


async def send_sla_reminder(
    to_email: str,
    recipient_name: str,
    policy_name: str,
    days_pending: int,
    dashboard_url: str,
) -> dict:
    """Send SLA reminder email for pending revision."""
    safe_recipient_name = html_text(recipient_name)
    safe_policy_name = html_text(policy_name)
    safe_dashboard_url = html_text(dashboard_url)

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #f59e0b; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: #fef3c7; padding: 20px; }}
        .footer {{ background-color: #78350f; color: white; padding: 15px; text-align: center; border-radius: 0 0 8px 8px; }}
        .button {{ background-color: #f59e0b; color: white; padding: 12px 24px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>SLA Reminder: Pending Policy Review</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p>This is a reminder that a policy revision has been waiting in the Review Queue for <strong>{days_pending} days</strong>:</p>
            <h2>{safe_policy_name}</h2>
            <p>Please review and take action as soon as possible.</p>
            <p><a href="{safe_dashboard_url}" class="button">Review Now</a></p>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(
        to_email=to_email,
        subject=f"SLA Reminder: {policy_name} Review Pending ({days_pending} days)",
        html=email_html,
    )
    if result.get("status") == "sent":
        logger.info(
            "sla_reminder_sent",
            to_email=to_email,
            policy_name=policy_name,
            days_pending=days_pending,
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_sla_reminder", error=result.get("error"))
    return result


async def send_escalation_notification(
    to_email: str,
    recipient_name: str,
    policy_name: str,
    escalation_days: int,
    assigned_reviewer: str,
    dashboard_url: str,
) -> dict:
    """Send escalation notification to CCO."""
    safe_recipient_name = html_text(recipient_name)
    safe_policy_name = html_text(policy_name)
    safe_assigned_reviewer = html_text(assigned_reviewer)
    safe_dashboard_url = html_text(dashboard_url)

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #dc2626; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: #fee2e2; padding: 20px; }}
        .footer {{ background-color: #7f1d1d; color: white; padding: 15px; text-align: center; border-radius: 0 0 8px 8px; }}
        .button {{ background-color: #dc2626; color: white; padding: 12px 24px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Escalation Alert: Review SLA Exceeded</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p>A Review Queue item has exceeded your escalation window of {escalation_days} days and needs compliance ownership:</p>
            <h2>{safe_policy_name}</h2>
            <p><strong>Assigned Reviewer:</strong> {safe_assigned_reviewer}</p>
            <p>Please review or reassign this policy revision immediately.</p>
            <p><a href="{safe_dashboard_url}" class="button">View Dashboard</a></p>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(
        to_email=to_email,
        subject=f"Escalation: {policy_name} Review SLA Exceeded",
        html=email_html,
    )
    if result.get("status") == "sent":
        logger.info(
            "escalation_notification_sent",
            to_email=to_email,
            policy_name=policy_name,
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_escalation_notification", error=result.get("error"))
    return result


async def send_decision_notification(
    to_email: str,
    recipient_name: str,
    policy_name: str,
    decision: str,
    reviewer_name: str,
    notes: str | None,
    dashboard_url: str,
) -> dict:
    """Send notification about a review decision."""
    decision_str = decision.capitalize()
    subject = f"Decision Made for {policy_name}: {decision_str}"
    header_color = "#10b981" if decision == "APPROVED" else "#ef4444"
    content_color = "#d1fae5" if decision == "APPROVED" else "#fee2e2"
    footer_color = "#047857" if decision == "APPROVED" else "#991b1b"
    safe_recipient_name = html_text(recipient_name)
    safe_policy_name = html_text(policy_name)
    safe_reviewer_name = html_text(reviewer_name)
    safe_notes = html_text(notes) if notes else None
    safe_dashboard_url = html_text(dashboard_url)

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: {header_color}; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: {content_color}; padding: 20px; }}
        .footer {{ background-color: {footer_color}; color: white; padding: 15px; text-align: center; border-radius: 0 0 8px 8px; }}
        .button {{ background-color: {header_color}; color: white; padding: 12px 24px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 20px; }}
        .notes {{ background-color: #ffffff; border-left: 4px solid #d1d5db; padding: 15px; margin-top: 15px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Policy Revision {decision_str}</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p>A decision has been recorded for the policy revision of <strong>{safe_policy_name}</strong>.</p>
            <p>The revision was <strong>{decision_str}</strong> by {safe_reviewer_name}.</p>
            {f'<div class="notes"><p><strong>Reviewer Notes:</strong></p><p>{safe_notes}</p></div>' if safe_notes else ""}
            <p><a href="{safe_dashboard_url}" class="button">View Details</a></p>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(to_email=to_email, subject=subject, html=email_html)
    if result.get("status") == "sent":
        logger.info(
            "decision_notification_sent",
            to_email=to_email,
            policy_name=policy_name,
            decision=decision,
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_decision_notification", error=result.get("error"))
    return result


async def send_reupload_reminder(
    to_email: str,
    recipient_name: str,
    policy_name: str,
    hours_since_approval: int,
    upload_url: str,
    *,
    is_immediate: bool = False,
    approved_on: str | None = None,
) -> dict:
    """Warn that an Approve-with-edits policy still needs Policy Library upload."""
    del approved_on  # reserved for future copy variants; screenshot template is fixed
    safe_recipient_name = html_text((recipient_name or "").strip() or "Colleague")
    safe_policy_name = html_text(policy_name)
    safe_upload_url = html_text(upload_url)

    if is_immediate:
        timing_line = (
            "Do this immediately. If the file is not uploaded soon, you will receive "
            "daily reminder emails until it is uploaded."
        )
        subject = f"WARNING: Upload edited {policy_name} to the Policy Library now"
        header = "WARNING: Upload your edited policy now"
    else:
        timing_line = (
            f"It has been about <strong>{hours_since_approval} hour(s)</strong> since approval. "
            "You will continue to receive this reminder every day until the upload is completed."
        )
        subject = f"Reminder: Upload edited {policy_name} policy to the Policy Library"
        header = "Reminder: Upload your edited policy"

    email_html = f"""
<html>
<head>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background-color: #d97706; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
        .content {{ background-color: #fffbeb; padding: 20px; }}
        .footer {{ background-color: #78350f; color: white; padding: 15px; text-align: center; border-radius: 0 0 8px 8px; }}
        .button {{ background-color: #d97706; color: white; padding: 12px 24px; border-radius: 4px; text-decoration: none; display: inline-block; margin-top: 20px; }}
        .warning {{ font-size: 18px; font-weight: 700; color: #92400e; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{header}</h1>
        </div>
        <div class="content">
            <p>Hi {safe_recipient_name},</p>
            <p class="warning">
                <strong>{safe_policy_name}</strong> is now <strong>Upload Pending</strong>.
            </p>
            <p>
                Reg AI needs you to upload the edited policy document to the Policy Library using the
                same policy name (<strong>{safe_policy_name}</strong>). After upload, the gap leaves
                Regulation Gap Analysis and is retained in Compliance History.
            </p>
            <p>{timing_line}</p>
            <p><a href="{safe_upload_url}" class="button">Upload to Policy Library</a></p>
        </div>
        <div class="footer">
            <p>&copy; 2026 Reg-AI. All rights reserved.</p>
        </div>
    </div>
</body>
</html>
"""

    result = send_resend_email(to_email=to_email, subject=subject, html=email_html)
    if result.get("status") == "sent":
        logger.info(
            "reupload_reminder_sent",
            to_email=to_email,
            policy_name=policy_name,
            hours_since_approval=hours_since_approval,
            is_immediate=is_immediate,
        )
    elif result.get("status") == "failed":
        logger.error("failed_to_send_reupload_reminder", error=result.get("error"))
    return result


def format_email_date(value: object | None) -> str | None:
    """Format a datetime (or parseable string) for customer-facing email copy."""
    if value is None:
        return None

    def _format(dt: object) -> str:
        month = dt.strftime("%B")  # type: ignore[union-attr]
        day = int(dt.day)  # type: ignore[union-attr]
        year = int(dt.year)  # type: ignore[union-attr]
        return f"{month} {day}, {year}"

    if hasattr(value, "strftime") and hasattr(value, "day") and hasattr(value, "year"):
        return _format(value)

    text = str(value).strip()
    if not text:
        return None
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return _format(parsed)
    except ValueError:
        return text


def build_policy_upload_url(frontend_url: str, policy_name: str) -> str:
    """Deep-link to the Policy Library Upload Policy dialog for a named policy."""
    from urllib.parse import quote

    base = frontend_url.rstrip("/")
    return f"{base}/policies?upload=1&policy_name={quote(policy_name)}"
