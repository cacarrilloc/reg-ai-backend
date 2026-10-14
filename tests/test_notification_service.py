from datetime import UTC
from unittest.mock import MagicMock, patch

import pytest

from app.services.notification_service import (
    build_policy_upload_url,
    format_email_date,
    send_decision_notification,
    send_resend_email,
    send_reupload_reminder,
)


@pytest.fixture
def mock_settings():
    """Fixture for mocking get_settings."""
    with patch("app.services.notification_service.get_settings") as mock:
        mock.return_value.resend_api_key = "test-key"
        mock.return_value.resend_from_email = "Reg-AI <notifications@alerts.ipxenterprises.com>"
        yield mock


@pytest.fixture
def mock_resend_send():
    """Fixture for mocking the Resend Emails.send helper path."""
    with patch("app.services.notification_service.send_resend_email") as mock_send:
        mock_send.return_value = {
            "status": "sent",
            "to": "cco@example.com",
            "message_id": "resend-id-123",
        }
        yield mock_send


@pytest.mark.asyncio
async def test_send_decision_notification_approved(mock_settings, mock_resend_send):
    """Test sending an 'APPROVED' decision notification."""
    result = await send_decision_notification(
        to_email="cco@example.com",
        recipient_name="CCO Bob",
        policy_name="Data Privacy Policy",
        decision="APPROVED",
        reviewer_name="Alice",
        notes="Looks good.",
        dashboard_url="http://localhost/dashboard",
    )

    assert result["status"] == "sent"
    assert result["message_id"] == "resend-id-123"
    mock_resend_send.assert_called_once()
    call_kwargs = mock_resend_send.call_args.kwargs
    assert call_kwargs["to_email"] == "cco@example.com"
    assert "Decision Made for Data Privacy Policy: Approved" in call_kwargs["subject"]
    assert "<h1>Policy Revision Approved</h1>" in call_kwargs["html"]
    assert "background-color: #10b981" in call_kwargs["html"]  # Green header
    assert "<strong>Reviewer Notes:</strong></p><p>Looks good.</p>" in call_kwargs["html"]


@pytest.mark.asyncio
async def test_send_decision_notification_rejected_no_notes(mock_settings, mock_resend_send):
    """Test sending a 'REJECTED' decision notification without notes."""
    result = await send_decision_notification(
        to_email="cco@example.com",
        recipient_name="CCO Bob",
        policy_name="Access Control Policy",
        decision="REJECTED",
        reviewer_name="Alice",
        notes=None,
        dashboard_url="http://localhost/dashboard",
    )

    assert result["status"] == "sent"
    mock_resend_send.assert_called_once()
    call_kwargs = mock_resend_send.call_args.kwargs
    assert "Decision Made for Access Control Policy: Rejected" in call_kwargs["subject"]
    assert "<h1>Policy Revision Rejected</h1>" in call_kwargs["html"]
    assert "background-color: #ef4444" in call_kwargs["html"]  # Red header
    assert "Reviewer Notes" not in call_kwargs["html"]


@pytest.mark.asyncio
async def test_send_decision_notification_no_api_key(mock_settings):
    """Test that notification is skipped if Resend API key is not configured."""
    mock_settings.return_value.resend_api_key = None

    result = await send_decision_notification(
        to_email="cco@example.com",
        recipient_name="CCO Bob",
        policy_name="Test Policy",
        decision="APPROVED",
        reviewer_name="Alice",
        notes=None,
        dashboard_url="http://localhost/dashboard",
    )

    assert result["status"] == "skipped"
    assert result["reason"] == "Resend API key not configured"


@pytest.mark.asyncio
async def test_send_decision_notification_resend_import_error(mock_settings):
    """Test that notification is skipped if Resend package is not installed."""
    with patch("app.services.notification_service.resend_sdk", None):
        result = await send_decision_notification(
            to_email="cco@example.com",
            recipient_name="CCO Bob",
            policy_name="Test Policy",
            decision="APPROVED",
            reviewer_name="Alice",
            notes=None,
            dashboard_url="http://localhost/dashboard",
        )

    assert result["status"] == "skipped"
    assert result["reason"] == "Resend package not installed"


@pytest.mark.asyncio
async def test_send_decision_notification_api_failure(mock_settings, mock_resend_send):
    """Test handling of an API error from Resend."""
    mock_resend_send.return_value = {"status": "failed", "error": "API is down"}

    result = await send_decision_notification(
        to_email="cco@example.com",
        recipient_name="CCO Bob",
        policy_name="Test Policy",
        decision="APPROVED",
        reviewer_name="Alice",
        notes=None,
        dashboard_url="http://localhost/dashboard",
    )

    assert result["status"] == "failed"
    assert result["error"] == "API is down"


def test_send_resend_email_uses_module_api(mock_settings):
    """Resend v2 exposes Emails.send on the module, not a Resend(...) client class."""
    mock_sdk = MagicMock()
    mock_sdk.Emails.send.return_value = {"id": "email_123"}
    with patch("app.services.notification_service.resend_sdk", mock_sdk):
        result = send_resend_email(
            to_email="user@example.com",
            subject="Hello",
            html="<p>Hi</p>",
        )

    assert result["status"] == "sent"
    assert result["message_id"] == "email_123"
    assert mock_sdk.api_key == "test-key"
    mock_sdk.Emails.send.assert_called_once()
    payload = mock_sdk.Emails.send.call_args[0][0]
    assert payload["to"] == ["user@example.com"]
    assert payload["subject"] == "Hello"


@pytest.mark.asyncio
async def test_send_reupload_reminder_immediate_copy(mock_settings, mock_resend_send):
    """Accept-with-edits sends the WARNING upload-pending email immediately."""
    result = await send_reupload_reminder(
        to_email="reviewer@example.com",
        recipient_name="IPX Inc",
        policy_name="VRM Test Policy",
        hours_since_approval=0,
        upload_url="http://localhost:3000/policies?upload=1&policy_name=VRM%20Test%20Policy",
        is_immediate=True,
        approved_on="July 18, 2026",
    )

    assert result["status"] == "sent"
    call_kwargs = mock_resend_send.call_args.kwargs
    assert call_kwargs["subject"] == (
        "WARNING: Upload edited VRM Test Policy to the Policy Library now"
    )
    html = call_kwargs["html"]
    assert "Hi IPX Inc," in html
    assert "WARNING: Upload your edited policy now" in html
    assert "Upload Pending" in html
    assert "Upload to Policy Library" in html
    assert "daily reminder emails" in html
    assert "policies?upload=1&amp;policy_name=VRM%20Test%20Policy" in html or (
        "policies?upload=1&policy_name=VRM%20Test%20Policy" in html
    )


@pytest.mark.asyncio
async def test_send_policy_review_request_explains_decision_options(
    mock_settings, mock_resend_send
):
    from app.services.notification_service import send_policy_review_request

    result = await send_policy_review_request(
        to_email="reviewer@example.com",
        recipient_name="Alex Reviewer",
        policy_name="AML Test Policy",
        dashboard_url="http://localhost:3000/reviews?revision_id=rev-1",
    )

    assert result["status"] == "sent"
    call_kwargs = mock_resend_send.call_args.kwargs
    assert call_kwargs["subject"] == "Assigned to review redline: AML Test Policy"
    html = call_kwargs["html"]
    assert "Hi Alex Reviewer," in html
    assert "Assigned to review redline" in html
    assert "assigned to review the <strong>AML Test Policy</strong> redline" in html
    assert "Accept all suggested changes" in html
    assert "Reject all suggested changes" in html
    assert "Accept with edits" in html
    assert "upload the final edited file" in html
    assert "Review in Dashboard" in html
    assert "Gap Summary" not in html
    assert "Upload Pending" not in html
    assert "Upload to Policy Library" not in html
    assert "Policy Review Required" not in html


@pytest.mark.asyncio
async def test_send_policy_review_request_uses_escalated_wording(
    mock_settings, mock_resend_send
):
    from app.services.notification_service import send_policy_review_request

    result = await send_policy_review_request(
        to_email="cco@example.com",
        recipient_name="Casey CCO",
        policy_name="AML Test Policy",
        dashboard_url="http://localhost:3000/reviews?revision_id=rev-1",
        escalated=True,
    )

    assert result["status"] == "sent"
    call_kwargs = mock_resend_send.call_args.kwargs
    assert call_kwargs["subject"] == "Escalated to review redline: AML Test Policy"
    html = call_kwargs["html"]
    assert "Escalated to review redline" in html
    assert "escalated to review the <strong>AML Test Policy</strong> redline" in html
    assert "Assigned to review redline" not in html
    assert "assigned to review" not in html


@pytest.mark.asyncio
async def test_send_revision_notification_shows_regulation_not_gap_summary(
    mock_settings, mock_resend_send
):
    from app.services.notification_service import send_revision_notification

    result = await send_revision_notification(
        to_email="reviewer@example.com",
        recipient_name="IPX Inc",
        regulation="FINRA Rule 3310; SEC Rule 17a-4",
        impact_score="HIGH",
        policy_name="AML Test Policy",
        dashboard_url="http://localhost:3000/reviews?revision_id=rev-1",
    )

    assert result["status"] == "sent"
    call_kwargs = mock_resend_send.call_args.kwargs
    assert call_kwargs["subject"] == "Policy Review Required: AML Test Policy"
    html = call_kwargs["html"]
    assert "Policy Review Required" in html
    assert "<strong>Regulation:</strong>" in html
    assert "FINRA Rule 3310; SEC Rule 17a-4" in html
    assert "Gap Summary" not in html
    assert "requiring CCO review" not in html


def test_build_policy_upload_url_encodes_policy_name() -> None:
    url = build_policy_upload_url("http://localhost:3000/", "VRM Test Policy")
    assert url == "http://localhost:3000/policies?upload=1&policy_name=VRM%20Test%20Policy"


def test_format_email_date_formats_readable_date() -> None:
    from datetime import datetime

    assert format_email_date(datetime(2026, 7, 18, 20, 0, tzinfo=UTC)) == "July 18, 2026"
    assert format_email_date(None) is None
