from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.review_jobs import check_escalations, check_sla_reminders

# Mark all tests in this module as asyncio
pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db_session():
    """Fixture for a mock SQLAlchemy session."""
    db_session = MagicMock()
    # Default to returning an empty list for queries
    db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = []
    with patch("app.workers.review_jobs.SessionLocal") as mock_session_local:
        mock_session_local.return_value = db_session
        yield db_session


@pytest.fixture
def mock_settings():
    """Fixture for mocking get_settings."""
    with patch("app.workers.review_jobs.get_settings") as mock:
        mock.return_value.sla_reminder_days = 3
        mock.return_value.frontend_url = "http://localhost:3000"
        yield mock


@pytest.fixture
def mock_send_sla_reminder():
    """Fixture for mocking send_sla_reminder."""
    with patch("app.workers.review_jobs.send_sla_reminder", new_callable=AsyncMock) as mock:
        mock.return_value = {"status": "sent"}
        yield mock


@pytest.fixture
def mock_send_escalation_notification():
    """Fixture for mocking send_escalation_notification."""
    with patch("app.workers.review_jobs.send_escalation_notification", new_callable=AsyncMock) as mock:
        mock.return_value = {"status": "sent"}
        yield mock


def create_test_objects(
    created_at,
    review_requested_at=None,
    sla_reminded_at=None,
    escalated_at=None,
    escalation_days=7,
):
    """Helper to create mock ORM objects for tests."""
    org = MagicMock()
    org.id = "org-1"
    org.auth_org_id = "org-1"
    org.escalation_days = escalation_days
    org.approval_chain = {
        "assigned_reviewer_email": "reviewer@example.com",
        "assigned_reviewer_name": "Reviewer Name",
        "cco_email": "cco@example.com",
        "cco_name": "CCO Name",
    }

    policy = MagicMock()
    policy.id = "policy-1"
    policy.name = "Test Policy"

    revision = MagicMock()
    revision.id = "rev-1"
    revision.org_id = org.auth_org_id
    revision.policy_id = policy.id
    revision.status = "PENDING"
    revision.created_at = created_at
    revision.review_requested_at = review_requested_at or created_at
    revision.sla_reminded_at = sla_reminded_at
    revision.escalated_at = escalated_at

    return revision, org, policy


# --- Tests for check_sla_reminders ---


async def test_check_sla_reminders_sends_reminder(mock_db_session, mock_settings, mock_send_sla_reminder):
    """Test that a reminder is sent for an old, un-reminded revision."""
    created_at = datetime.utcnow() - timedelta(days=4)
    revision, org, policy = create_test_objects(created_at=created_at)
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_sla_reminders()

    assert result["status"] == "completed"
    assert result["reminded_count"] == 1
    mock_send_sla_reminder.assert_called_once()
    mock_db_session.add.assert_called_with(revision)
    mock_db_session.commit.assert_called_once()
    assert isinstance(revision.sla_reminded_at, datetime)


async def test_check_sla_reminders_falls_back_to_cco(mock_db_session, mock_settings, mock_send_sla_reminder):
    """Unassigned queue items still remind the CCO."""
    created_at = datetime.utcnow() - timedelta(days=4)
    revision, org, policy = create_test_objects(created_at=created_at)
    org.approval_chain = {"cco_email": "cco@example.com", "cco_name": "CCO Name"}
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_sla_reminders()

    assert result["status"] == "completed"
    assert result["reminded_count"] == 1
    mock_send_sla_reminder.assert_called_once()
    assert mock_send_sla_reminder.await_args.kwargs["to_email"] == "cco@example.com"


async def test_check_sla_reminders_skips_recent_revision(mock_db_session, mock_settings, mock_send_sla_reminder):
    """Test that no reminder is sent for a revision within the SLA period."""
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = []

    result = await check_sla_reminders()

    assert result["status"] == "completed"
    assert result["reminded_count"] == 0
    mock_send_sla_reminder.assert_not_called()
    mock_db_session.commit.assert_called_once()


async def test_check_sla_reminders_no_reviewer_email(mock_db_session, mock_settings, mock_send_sla_reminder):
    """Test that no reminder is sent if the reviewer email is missing."""
    created_at = datetime.utcnow() - timedelta(days=4)
    revision, org, policy = create_test_objects(created_at=created_at)
    org.approval_chain = {}  # No email
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_sla_reminders()

    assert result["status"] == "completed"
    assert result["reminded_count"] == 0
    mock_send_sla_reminder.assert_not_called()
    mock_db_session.commit.assert_called_once()


async def test_check_sla_reminders_handles_exception(mock_db_session, mock_settings, mock_send_sla_reminder):
    """Test that the job handles exceptions gracefully and rolls back the DB."""
    created_at = datetime.utcnow() - timedelta(days=4)
    revision, org, policy = create_test_objects(created_at=created_at)
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]
    mock_send_sla_reminder.side_effect = Exception("SMTP server down")

    result = await check_sla_reminders()

    assert result["status"] == "failed"
    assert result["error"] == "SMTP server down"
    mock_db_session.rollback.assert_called_once()
    mock_db_session.commit.assert_not_called()


# --- Tests for check_escalations ---


async def test_check_escalations_sends_escalation(mock_db_session, mock_settings, mock_send_escalation_notification):
    """Test that an escalation is sent for an overdue, un-escalated revision."""
    created_at = datetime.utcnow() - timedelta(days=8)
    revision, org, policy = create_test_objects(created_at=created_at, escalation_days=7)
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_escalations()

    assert result["status"] == "completed"
    assert result["escalated_count"] == 1
    mock_send_escalation_notification.assert_called_once()
    mock_db_session.add.assert_called_with(revision)
    mock_db_session.commit.assert_called_once()
    assert isinstance(revision.escalated_at, datetime)


async def test_check_escalations_uses_review_requested_at(
    mock_db_session, mock_settings, mock_send_escalation_notification
):
    """Escalation age starts when the item entered the Review Queue."""
    created_at = datetime.utcnow() - timedelta(days=20)
    review_requested_at = datetime.utcnow() - timedelta(days=2)
    revision, org, policy = create_test_objects(
        created_at=created_at,
        review_requested_at=review_requested_at,
        escalation_days=7,
    )
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_escalations()

    assert result["status"] == "completed"
    assert result["escalated_count"] == 0
    mock_send_escalation_notification.assert_not_called()


async def test_check_escalations_skips_recent_revision(
    mock_db_session, mock_settings, mock_send_escalation_notification
):
    """Test that no escalation is sent for a revision within the escalation period."""
    created_at = datetime.utcnow() - timedelta(days=5)
    revision, org, policy = create_test_objects(created_at=created_at, escalation_days=7)
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_escalations()

    assert result["status"] == "completed"
    assert result["escalated_count"] == 0
    mock_send_escalation_notification.assert_not_called()
    mock_db_session.commit.assert_called_once()


async def test_check_escalations_no_cco_email(mock_db_session, mock_settings, mock_send_escalation_notification):
    """Test that no escalation is sent if the CCO email is missing."""
    created_at = datetime.utcnow() - timedelta(days=8)
    revision, org, policy = create_test_objects(created_at=created_at, escalation_days=7)
    org.approval_chain = {}  # No CCO email
    mock_db_session.query.return_value.join.return_value.join.return_value.filter.return_value.all.return_value = [
        (revision, org, policy)
    ]

    result = await check_escalations()

    assert result["status"] == "completed"
    assert result["escalated_count"] == 0
    mock_send_escalation_notification.assert_not_called()
    mock_db_session.commit.assert_called_once()
