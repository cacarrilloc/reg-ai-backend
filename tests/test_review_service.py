from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest

import app.services.organization_service as organization_service
import app.services.review_service as review_service
from app.core.config import Settings
from app.core.security import CurrentUser
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
from app.schemas.review import AssignReviewerRequest, ReviewDecisionRequest


class Query:
    def __init__(self, result: object) -> None:
        self.result = result
        self.filters: list[object] = []
        self.aggregate_pending_counts = False

    def filter(self, *criteria: object) -> object:
        query = Query(self.result)
        query.filters = [*self.filters, *criteria]
        return query

    def join(self, *_args: object) -> object:
        return self

    def outerjoin(self, *_args: object) -> object:
        return self

    def options(self, *_args: object) -> object:
        return self

    def order_by(self, *_args: object) -> object:
        return self

    def limit(self, *_args: object) -> object:
        return self

    def with_entities(self, *_args: object) -> object:
        query = Query(self.result)
        query.filters = self.filters
        query.aggregate_pending_counts = True
        return query

    def one(self) -> object:
        if self.aggregate_pending_counts and isinstance(self.result, list):
            return (
                len(self.result),
                sum(
                    1
                    for row in self.result
                    if isinstance(row, tuple)
                    and len(row) > 1
                    and getattr(row[1], "impact_score", None) == "HIGH"
                ),
                sum(
                    1
                    for row in self.result
                    if isinstance(row, tuple)
                    and row
                    and getattr(row[0], "escalated_at", None) is not None
                ),
            )
        return self.result

    def one_or_none(self) -> object:
        return self.result

    def first(self) -> object:
        return self.result

    def all(self) -> object:
        return self.result if isinstance(self.result, list) else [self.result]

    def count(self) -> int:
        text = " ".join(str(item) for item in self.filters)
        if "impact_score" in text:
            return 1
        if "escalated_at" in text:
            return 1
        return len(self.result) if isinstance(self.result, list) else int(bool(self.result))


class RecordingSession:
    def __init__(self, query_map: dict[object, object]) -> None:
        self.query_map = query_map
        self.added: list[object] = []
        self.committed = False
        self.refreshed: list[object] = []

    def query(self, *entities: object) -> Query:
        if not entities:
            return Query(None)
        if len(entities) > 1:
            result = self.query_map.get(tuple(entities))
            if result is not None:
                return Query(result)
        return Query(self.query_map.get(entities[0]))

    def add(self, value: object) -> None:
        self.added.append(value)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, value: object) -> None:
        self.refreshed.append(value)

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_record_review_decision_approved_creates_audit_log_and_notification(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/policy-revisions/rev-1/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="FINRA",
        version=3,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "cco_email": "cco@example.com",
            "cco_name": "CCO",
            "assigned_reviewer_email": "reviewer@example.com",
            "assigned_reviewer_name": "Reviewer",
        },
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    mock_notify = AsyncMock(return_value={"status": "sent"})
    mock_reupload = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(review_service, "send_decision_notification", mock_notify)
    monkeypatch.setattr(review_service, "send_reupload_reminder", mock_reupload)
    monkeypatch.setattr(review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000"))
    monkeypatch.setattr(
        review_service,
        "organization_notification_recipients",
        lambda *_args, **_kwargs: [
            organization_service.OrganizationMemberContact(
                user_id="user-a",
                email="ada@example.com",
                full_name="Ada Lovelace",
            ),
            organization_service.OrganizationMemberContact(
                user_id="user-b",
                email="grace@example.com",
                full_name="Grace Hopper",
            ),
        ],
    )

    response = await review_service.record_review_decision(
        "rev-1",
        ReviewDecisionRequest(decision="APPROVED_WITH_EDITS", notes="Looks good."),
        CurrentUser(
            user_id="user-1",
            org_id="org_1",
            role="chief_compliance_officer",
            claims={"email": "actor@example.com", "name": "Carlos Carrillo"},
        ),
        db,  # type: ignore[arg-type]
    )

    audit_logs = [item for item in db.added if isinstance(item, AuditLog)]
    assert response.status == "APPROVED"
    assert response.policy_version is None
    assert response.requires_policy_upload is True
    assert response.approval_mode == "WITH_EDITS"
    assert response.policy_name == "FINRA"
    assert revision.status == "APPROVED"
    assert revision.reviewer_id == "user-1"
    assert revision.reuploaded_at is None
    assert revision.reupload_reminded_at is not None
    assert len(audit_logs) == 1
    assert audit_logs[0].actor_id == "user-1"
    assert audit_logs[0].metadata_json["decision"] == "APPROVED"
    assert audit_logs[0].metadata_json["approval_mode"] == "WITH_EDITS"
    assert audit_logs[0].metadata_json["requires_policy_upload"] is True
    assert audit_logs[0].metadata_json["reupload_notify_email"] == "actor@example.com"
    assert audit_logs[0].metadata_json["reupload_notify_name"] == "Carlos Carrillo"
    assert mock_notify.await_count == 2
    assert {call.kwargs["to_email"] for call in mock_notify.await_args_list} == {
        "ada@example.com",
        "grace@example.com",
    }
    assert mock_notify.call_args.kwargs["dashboard_url"] == "http://localhost:3000/reviews?revision_id=rev-1"
    mock_reupload.assert_awaited_once()
    assert mock_reupload.call_args.kwargs["to_email"] == "actor@example.com"
    assert mock_reupload.call_args.kwargs["is_immediate"] is True
    assert "policies?upload=1&policy_name=FINRA" in mock_reupload.call_args.kwargs["upload_url"]


@pytest.mark.asyncio
async def test_assign_revision_reviewer_persists_and_emails(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-assign",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/policy-revisions/rev-assign/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="VRM Test Policy",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    mock_email = AsyncMock(
        return_value={
            "status": "sent",
            "subject": "Assigned to review redline: VRM Test Policy",
            "email_kind": "policy_review_request",
        }
    )
    monkeypatch.setattr(review_service, "send_policy_review_request", mock_email)
    monkeypatch.setattr(
        review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000")
    )

    response = await review_service.assign_revision_reviewer(
        "rev-assign",
        AssignReviewerRequest(
            user_id="user_reviewer",
            email="reviewer@example.com",
            full_name="Alex Reviewer",
        ),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )

    assert response.email_status == "sent"
    assert response.policy_name == "VRM Test Policy"
    assert response.reviewer_email == "reviewer@example.com"
    assert response.email_kind == "policy_review_request"
    assert response.email_subject == "Assigned to review redline: VRM Test Policy"
    assert org.approval_chain["assigned_reviewer_user_id"] == "user_reviewer"
    assert org.approval_chain["assigned_reviewer_email"] == "reviewer@example.com"
    assert revision.reviewer_id == "user_reviewer"
    assert revision.assigned_reviewer_name == "Alex Reviewer"
    assert revision.assigned_reviewer_email == "reviewer@example.com"
    mock_email.assert_awaited_once()
    assert mock_email.call_args.kwargs["to_email"] == "reviewer@example.com"
    assert mock_email.call_args.kwargs["policy_name"] == "VRM Test Policy"
    assert mock_email.call_args.kwargs["escalated"] is False
    assert "revision_id=rev-assign" in mock_email.call_args.kwargs["dashboard_url"]
    assert response.escalated is False
    assert revision.escalated_at is None


@pytest.mark.asyncio
async def test_escalate_revision_reviewer_requires_existing_assignee(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-escalate",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id=None,
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="VRM Test Policy",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    monkeypatch.setattr(review_service, "send_policy_review_request", AsyncMock())
    monkeypatch.setattr(
        review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000")
    )

    with pytest.raises(review_service.HTTPException) as caught:
        await review_service.assign_revision_reviewer(
            "rev-escalate",
            AssignReviewerRequest(
                user_id="user_cco",
                email="cco@example.com",
                full_name="Casey CCO",
                escalate=True,
            ),
            CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
            db,  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 400
    assert "already assigned" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_escalate_revision_reviewer_reassigns_and_stamps_escalated_at(
    monkeypatch,
) -> None:
    revision = PolicyRevision(
        id="rev-escalate",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id="user_reviewer",
        assigned_reviewer_name="Alex Reviewer",
        assigned_reviewer_email="reviewer@example.com",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="VRM Test Policy",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "assigned_reviewer_user_id": "user_reviewer",
            "assigned_reviewer_email": "reviewer@example.com",
            "assigned_reviewer_name": "Alex Reviewer",
        },
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    mock_email = AsyncMock(
        return_value={
            "status": "sent",
            "subject": "Assigned to review redline: VRM Test Policy",
            "email_kind": "policy_review_request",
        }
    )
    monkeypatch.setattr(review_service, "send_policy_review_request", mock_email)
    monkeypatch.setattr(
        review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000")
    )

    response = await review_service.assign_revision_reviewer(
        "rev-escalate",
        AssignReviewerRequest(
            user_id="user_cco",
            email="cco@example.com",
            full_name="Casey CCO",
            escalate=True,
        ),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )

    assert response.escalated is True
    assert response.reviewer_user_id == "user_cco"
    assert response.reviewer_email == "cco@example.com"
    assert revision.reviewer_id == "user_cco"
    assert revision.assigned_reviewer_name == "Casey CCO"
    assert revision.escalated_at is not None
    assert response.escalated_at == revision.escalated_at
    mock_email.assert_awaited_once()
    assert mock_email.call_args.kwargs["to_email"] == "cco@example.com"
    assert mock_email.call_args.kwargs["escalated"] is True


@pytest.mark.asyncio
async def test_escalate_revision_reviewer_retries_when_escalated_at_cannot_be_saved(
    monkeypatch,
) -> None:
    revision = PolicyRevision(
        id="rev-escalate",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id="user_reviewer",
        assigned_reviewer_name="Alex Reviewer",
        assigned_reviewer_email="reviewer@example.com",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="VRM Test Policy",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "assigned_reviewer_user_id": "user_reviewer",
            "assigned_reviewer_email": "reviewer@example.com",
            "assigned_reviewer_name": "Alex Reviewer",
        },
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    original_commit = db.commit
    calls = {"count": 0}

    def flaky_commit() -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError('column "escalated_at" does not exist')
        original_commit()

    db.commit = flaky_commit  # type: ignore[method-assign]
    monkeypatch.setattr(
        review_service,
        "send_policy_review_request",
        AsyncMock(return_value={"status": "sent", "subject": "Escalated to review redline: VRM Test Policy"}),
    )
    monkeypatch.setattr(
        review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000")
    )

    response = await review_service.assign_revision_reviewer(
        "rev-escalate",
        AssignReviewerRequest(
            user_id="user_cco",
            email="cco@example.com",
            full_name="Casey CCO",
            escalate=True,
        ),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )

    assert calls["count"] == 2
    assert response.escalated is True
    assert revision.reviewer_id == "user_cco"


@pytest.mark.asyncio
async def test_escalate_revision_reviewer_rejects_same_assignee(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-escalate",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id="user_reviewer",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="VRM Test Policy",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
        }
    )
    monkeypatch.setattr(review_service, "send_policy_review_request", AsyncMock())
    monkeypatch.setattr(
        review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000")
    )

    with pytest.raises(review_service.HTTPException) as caught:
        await review_service.assign_revision_reviewer(
            "rev-escalate",
            AssignReviewerRequest(
                user_id="user_reviewer",
                email="reviewer@example.com",
                full_name="Alex Reviewer",
                escalate=True,
            ),
            CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
            db,  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 400
    assert "different organization member" in str(caught.value.detail)


def test_review_queue_assigned_badge_is_per_revision_not_org_sticky() -> None:
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "assigned_reviewer_user_id": "user_reviewer",
            "assigned_reviewer_email": "reviewer@example.com",
            "assigned_reviewer_name": "Alex Reviewer",
        },
    )
    unassigned = PolicyRevision(
        id="rev-unassigned",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id=None,
    )
    assigned = PolicyRevision(
        id="rev-assigned",
        gap_analysis_id="gap-2",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id="user_reviewer",
        assigned_reviewer_name="Alex Reviewer",
        assigned_reviewer_email="reviewer@example.com",
    )
    older_assignee = PolicyRevision(
        id="rev-older",
        gap_analysis_id="gap-3",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        reviewer_id="user_other",
        assigned_reviewer_name="Other Reviewer",
        assigned_reviewer_email="other@example.com",
    )

    assert review_service.revision_has_assigned_reviewer(unassigned) is False
    assert review_service.revision_reviewer_name(unassigned, org) is None
    assert review_service.revision_reviewer_email(unassigned, org) is None

    assert review_service.revision_has_assigned_reviewer(assigned) is True
    assert review_service.revision_reviewer_name(assigned, org) == "Alex Reviewer"
    assert review_service.revision_reviewer_email(assigned, org) == "reviewer@example.com"
    # Per-revision columns win even when org sticky points at someone else.
    assert review_service.revision_reviewer_name(older_assignee, org) == "Other Reviewer"
    assert review_service.revision_reviewer_email(older_assignee, org) == "other@example.com"
    # Org sticky preference alone must not imply the queue item was assigned.
    assert review_service.has_assigned_reviewer(org) is True


@pytest.mark.asyncio
async def test_record_review_decision_approve_all_changes_auto_uploads(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-all",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/policy-revisions/rev-all/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="FINRA",
        version=3,
        status="ACTIVE",
    )
    gap = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        trigger_type="policy_update",
        trigger_id="policy-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Gap",
        rule="FINRA Rule 3110",
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={"cco_email": "cco@example.com", "cco_name": "CCO"},
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
            GapAnalysis: gap,
        }
    )

    monkeypatch.setattr(review_service, "send_decision_notification", AsyncMock(return_value={"status": "sent"}))
    monkeypatch.setattr(review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000"))
    monkeypatch.setattr(review_service, "download_file", lambda _url: b"redline-bytes")
    monkeypatch.setattr(
        "app.services.tracked_redline_document.accept_all_tracked_changes",
        lambda _bytes: b"clean-bytes",
    )

    async def fake_increment(*_args: object, **_kwargs: object) -> tuple[int, str]:
        assert _kwargs.get("approved_bytes") == b"clean-bytes"
        return 4, "storage://policies/org_1/policies/new/v4.docx"

    monkeypatch.setattr(
        "app.services.policy_service.increment_policy_version",
        fake_increment,
    )
    history_calls: list[str] = []

    def fake_history(_db: object, gap_row: GapAnalysis) -> None:
        history_calls.append(gap_row.id)

    monkeypatch.setattr(
        "app.services.compliance_history_service.record_history_from_gap_row",
        fake_history,
    )

    response = await review_service.record_review_decision(
        "rev-all",
        ReviewDecisionRequest(decision="APPROVED_ALL_CHANGES"),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )

    assert response.status == "APPROVED"
    assert response.approval_mode == "ALL_CHANGES"
    assert response.requires_policy_upload is False
    assert response.policy_version == 4
    assert response.approved_file_url == "storage://policies/org_1/policies/new/v4.docx"
    assert revision.status == "APPROVED"
    assert revision.reuploaded_at is not None
    assert revision.approved_file_url == response.approved_file_url
    assert history_calls == ["gap-1"]


@pytest.mark.asyncio
async def test_get_pending_revisions_includes_summary_and_trigger_details() -> None:
    publication = RegulatoryPublication(
        id="pub-1",
        source="FINRA",
        title="FINRA Regulatory Notice",
        url="https://example.com/notice",
        published_at=datetime.utcnow(),
        raw_text="New rule text",
    )
    business_change = BusinessChange(
        id="change-1",
        org_id="org_1",
        change_type="NEW_PRODUCT",
        description="Launches a new retail product",
        flagged_by="user-1",
        change_date=datetime.utcnow().date(),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Policy One",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "assigned_reviewer_email": "reviewer@example.com",
            "assigned_reviewer_name": "Reviewer",
            "cco_email": "cco@example.com",
            "cco_name": "CCO",
        },
        escalation_days=5,
    )
    gap_publication = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        publication_id="pub-1",
        trigger_type="publication",
        trigger_id="pub-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Policy language should reference the FINRA notice.",
        regulatory_citations=[],
    )
    gap_change = GapAnalysis(
        id="gap-2",
        org_id="org_1",
        business_change_id="change-1",
        trigger_type="business_change",
        trigger_id="change-1",
        policy_id="policy-1",
        impact_score="MEDIUM",
        gap_summary="Policy needs a review for the new product.",
        regulatory_citations=[],
    )
    gap_policy_update = GapAnalysis(
        id="gap-3",
        org_id="org_1",
        trigger_type="policy_update",
        trigger_id="policy-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Uploaded policy is missing AML escalation language.",
        rule="FINRA Rule 3310 requires written AML procedures.",
        regulatory_citations=[
            {"source": "FINRA", "reference": "Rule 3310"},
        ],
        retrieval_metadata={
            "regulator_reference_policy_name": "FINRA AML Reference",
        },
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    recent_revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-1/redline.docx",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        created_at=datetime.utcnow() - timedelta(days=2),
    )
    older_revision = PolicyRevision(
        id="rev-2",
        gap_analysis_id="gap-2",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-2/redline.docx",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        escalated_at=datetime.utcnow(),
        created_at=datetime.utcnow() - timedelta(days=8),
    )
    policy_update_revision = PolicyRevision(
        id="rev-3",
        gap_analysis_id="gap-3",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-3/redline.docx",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    rows = [
        (recent_revision, gap_publication, policy, publication, None, org),
        (older_revision, gap_change, policy, None, business_change, org),
        (policy_update_revision, gap_policy_update, policy, None, None, org),
    ]
    db = RecordingSession({PolicyRevision: rows})

    result = await review_service.get_pending_revisions("org_1", db)  # type: ignore[arg-type]

    assert result.total_pending == 3
    assert result.escalated_count == 1
    assert sum(1 for item in result.pending_revisions if item.impact_score == "HIGH") == 2
    assert result.pending_revisions[0].trigger_type == "publication"
    assert result.pending_revisions[0].trigger_label == "FINRA Regulatory Notice"
    assert result.pending_revisions[0].regulatory_publication == (
        "FINRA · FINRA Regulatory Notice"
    )
    assert result.pending_revisions[0].business_change is None
    assert result.pending_revisions[1].trigger_type == "business_change"
    assert result.pending_revisions[1].trigger_label == "Launches a new retail product"
    assert result.pending_revisions[1].regulatory_publication is None
    assert result.pending_revisions[1].business_change == "Launches a new retail product"
    assert result.pending_revisions[1].change_date == business_change.change_date
    assert result.pending_revisions[1].gap_summary == "Policy needs a review for the new product."
    assert result.pending_revisions[0].gap_count == 1
    assert result.pending_revisions[0].gaps[0].summary == (
        "Policy language should reference the FINRA notice."
    )
    assert result.pending_revisions[1].reviewer_name is None
    assert result.pending_revisions[1].reviewer_email is None
    assert result.pending_revisions[1].has_assigned_reviewer is False
    assert result.pending_revisions[0].has_assigned_reviewer is False
    assert result.pending_revisions[2].has_assigned_reviewer is False
    assert result.pending_revisions[1].escalation_days == 5
    assert result.pending_revisions[2].trigger_type == "policy_update"
    assert result.pending_revisions[2].regulatory_publication == (
        "FINRA Rule 3310 requires written AML procedures."
    )
    assert result.pending_revisions[2].marketing_rule_labels == []


@pytest.mark.asyncio
async def test_get_pending_revisions_list_view_skips_finding_bodies(monkeypatch) -> None:
    publication = RegulatoryPublication(
        id="pub-1",
        source="FINRA",
        title="FINRA Regulatory Notice",
        url="https://example.com/notice",
        published_at=datetime.utcnow(),
        raw_text="x" * 50_000,
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Policy One",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
        escalation_days=5,
    )
    gap = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        publication_id="pub-1",
        trigger_type="publication",
        trigger_id="pub-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Policy language should reference the FINRA notice.",
        rule="FINRA Rule 3110 requires supervision.",
        regulatory_citations=[{"source": "FINRA", "reference": "Rule 3110"}],
    )
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-1/redline.docx",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )

    def fail_if_called(*_args: object, **_kwargs: object):
        raise AssertionError("list view must not parse RULE/GAP/FIX finding bodies")

    monkeypatch.setattr(
        "app.services.gap_analysis_service.build_gap_finding_summaries",
        fail_if_called,
    )

    db = RecordingSession(
        {
            PolicyRevision: [
                (revision, gap, policy, publication, None, org),
            ]
        }
    )
    result = await review_service.get_pending_revisions(
        "org_1",
        db,  # type: ignore[arg-type]
        include_findings=False,
    )

    assert result.total_pending == 1
    assert result.pending_revisions[0].gaps == []
    assert result.pending_revisions[0].rule is None
    assert result.pending_revisions[0].fix is None
    assert result.pending_revisions[0].regulatory_publication == (
        "FINRA · FINRA Regulatory Notice"
    )
    assert result.pending_revisions[0].gap_summary == (
        "Policy language should reference the FINRA notice."
    )[:280]


def test_publication_queue_defers_skips_unmigrated_feed_summary() -> None:
    keys = [
        repr(option._generate_cache_key())
        for option in review_service._publication_queue_defers()
    ]
    assert any("feed_summary" in key for key in keys)


@pytest.mark.asyncio
async def test_get_pending_revisions_retries_after_missing_column() -> None:
    from sqlalchemy.exc import ProgrammingError

    publication = RegulatoryPublication(
        id="pub-1",
        source="FINRA",
        title="FINRA Regulatory Notice",
        url="https://example.com/notice",
        published_at=datetime.utcnow(),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Policy One",
        version=2,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
        escalation_days=5,
    )
    gap = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        publication_id="pub-1",
        trigger_type="publication",
        trigger_id="pub-1",
        policy_id="policy-1",
        impact_score="MEDIUM",
        gap_summary="Need a citation.",
    )
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    rows = [(revision, gap, policy, publication, None, org)]

    class FlakyQuery(Query):
        failures = 1

        def all(self) -> object:
            if FlakyQuery.failures > 0:
                FlakyQuery.failures -= 1
                raise ProgrammingError(
                    "SELECT",
                    {},
                    Exception('column "feed_summary" does not exist'),
                )
            return super().all()

    class FlakySession(RecordingSession):
        def query(self, *entities: object) -> FlakyQuery:
            if len(entities) > 1:
                mapped = self.query_map.get(tuple(entities))
                if mapped is not None:
                    return FlakyQuery(mapped)
            return FlakyQuery(self.query_map.get(entities[0]))

    result = await review_service.get_pending_revisions(
        "org_1",
        FlakySession({PolicyRevision: rows}),  # type: ignore[arg-type]
        include_findings=False,
    )

    assert result.total_pending == 1
    assert result.pending_revisions[0].revision_id == "rev-1"


@pytest.mark.asyncio
async def test_get_pending_revisions_shows_marketing_affected_rules() -> None:
    policy = Policy(
        id="policy-mkt",
        org_id="org_1",
        name="Presentation — Reg-AI Pitch Deck",
        version=1,
        status="ACTIVE",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
        escalation_days=5,
    )
    gap = GapAnalysis(
        id="gap-mkt",
        org_id="org_1",
        trigger_type="marketing_content",
        trigger_id="content-1",
        policy_id="policy-mkt",
        impact_score="HIGH",
        gap_summary="Pitch deck findings",
        rule="206(4)-1",
        regulatory_citations=[],
        retrieval_metadata={
            "marketing_format": "presentation",
            "marketing_rule_codes": ["206(4)-1", "204-2", "2210", "2211-2216"],
            "sentence_findings": [
                {
                    "rule": "206(4)-1",
                    "gap": "Missing performance disclosure.",
                    "fix": "Add net-of-fees performance.",
                    "impact_score": "HIGH",
                }
            ],
        },
        created_at=datetime.utcnow(),
    )
    revision = PolicyRevision(
        id="rev-mkt",
        gap_analysis_id="gap-mkt",
        policy_id="policy-mkt",
        org_id="org_1",
        status="PENDING",
        review_requested_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db = RecordingSession(
        {
            PolicyRevision: [
                (revision, gap, policy, None, None, org),
            ]
        }
    )

    result = await review_service.get_pending_revisions("org_1", db)  # type: ignore[arg-type]

    assert result.pending_revisions[0].policy_name == "Presentation: Reg-AI Pitch Deck"
    assert result.pending_revisions[0].trigger_type == "marketing_content"
    assert result.pending_revisions[0].marketing_rule_labels == [
        "SEC: Rule 206(4)-1",
        "SEC: Rule 204-2",
        "FINRA: Rule 2210",
        "FINRA: Rules 2211–2216",
    ]
    assert result.pending_revisions[0].regulatory_publication == (
        "SEC: Rule 206(4)-1\n"
        "SEC: Rule 204-2\n"
        "FINRA: Rule 2210\n"
        "FINRA: Rules 2211–2216"
    )


def test_regulatory_publication_label_uses_sentence_finding_rules() -> None:
    gap = GapAnalysis(
        id="gap-vrm",
        org_id="org_1",
        trigger_type="policy_update",
        trigger_id="policy-vrm",
        policy_id="policy-vrm",
        impact_score="HIGH",
        gap_summary="Multiple findings",
        rule="FINRA Rule 3110",
        regulatory_citations=[],
        retrieval_metadata={
            "sentence_findings": [
                {
                    "rule": "FINRA Rule 3120 (Supervisory Control System)",
                    "gap": "Missing principal designation.",
                    "fix": "Add principal designation.",
                    "impact_score": "MEDIUM",
                },
                {
                    "rule": (
                        "SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30) and "
                        "SEC Rule 10 (Incident Reporting for Covered Entities)"
                    ),
                    "gap": "Missing incident evaluation.",
                    "fix": "Add incident evaluation.",
                    "impact_score": "MEDIUM",
                },
            ]
        },
    )

    label = review_service.regulatory_publication_label(gap, None)

    assert label == (
        "FINRA Rule 3120 (Supervisory Control System); "
        "SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30) and "
        "SEC Rule 10 (Incident Reporting for Covered Entities)"
    )


def test_regulatory_publication_label_falls_back_to_gap_rules() -> None:
    gap = GapAnalysis(
        id="gap-vrm",
        org_id="org_1",
        trigger_type="policy_update",
        trigger_id="policy-vrm",
        policy_id="policy-vrm",
        impact_score="HIGH",
        gap_summary="Missing supervisory controls.",
        rule="FINRA Rule 3110",
        regulatory_citations=[{"source": "FINRA", "reference": "Rule 3110"}],
        retrieval_metadata={},
    )

    label = review_service.regulatory_publication_label(gap, None)

    assert label == "FINRA Rule 3110"


@pytest.mark.asyncio
async def test_get_redline_download_content_returns_docx_bytes(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-1/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Electronic Communications Policy",
        type="uploaded",
        version=1,
        status="ACTIVE",
    )
    db = RecordingSession({(PolicyRevision, Policy): (revision, policy)})
    monkeypatch.setattr(review_service, "download_file", lambda _url: b"docx-bytes")

    content, filename, content_type = review_service.get_redline_download_content(
        "rev-1",
        "org_1",
        db,  # type: ignore[arg-type]
    )

    assert content == b"docx-bytes"
    assert filename == "redline.docx"
    assert content_type.endswith("wordprocessingml.document")


@pytest.mark.asyncio
async def test_get_redline_download_url_returns_signed_url(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-1/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow(),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Electronic Communications Policy",
        type="uploaded",
        version=1,
        status="ACTIVE",
    )
    db = RecordingSession({(PolicyRevision, Policy): (revision, policy)})
    monkeypatch.setattr(review_service, "create_download_url", lambda _url: "https://signed.example/redline.docx")

    result = review_service.get_redline_download_url("rev-1", "org_1", db)  # type: ignore[arg-type]

    assert result == "https://signed.example/redline.docx"


def test_get_policy_audit_chains_returns_full_policy_lifecycle() -> None:
    business_change = BusinessChange(
        id="change-1",
        org_id="org_1",
        change_type="NEW_PRODUCT",
        description="Launch a structured note program",
        flagged_by="user-1",
        change_date=datetime.utcnow().date(),
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="Written Supervisory Procedures",
        version=3,
        status="ACTIVE",
    )
    change_gap = GapAnalysis(
        id="gap-2",
        org_id="org_1",
        business_change_id="change-1",
        trigger_type="business_change",
        trigger_id="change-1",
        policy_id="policy-1",
        impact_score="MEDIUM",
        gap_summary="WSP should address structured note review.",
        rule="FINRA Rule 3110 requires supervisory controls.",
        regulatory_citations=[
            {
                "source": "FINRA",
                "reference": "Rule 3110",
                "quote": "Supervisory systems are required.",
            }
        ],
        retrieval_metadata={
            "regulator_document_similarity": [
                {"policy_name": "FINRA Regulations", "cosine_similarity": 0.75}
            ],
            "sentence_findings": [
                {
                    "sentence": "The firm will review structured notes.",
                    "rule": "FINRA Rule 3110",
                    "gap": "Missing supervisory review cadence for structured notes.",
                    "fix": "Add annual WSP review of structured note suitability.",
                    "impact_score": "MEDIUM",
                },
                {
                    "sentence": "Product approvals are informal.",
                    "rule": "FINRA Rule 2111",
                    "gap": "No documented suitability analysis process.",
                    "fix": "Require written suitability reviews before launch.",
                    "impact_score": "HIGH",
                },
            ],
        },
        created_at=datetime.utcnow(),
    )
    pending_revision = PolicyRevision(
        id="rev-2",
        gap_analysis_id="gap-2",
        policy_id="policy-1",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/rev-2/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    rows = [
        (change_gap, policy, None, business_change, pending_revision, None),
    ]
    db = RecordingSession({GapAnalysis: rows})

    result = review_service.get_policy_audit_chains("org_1", db)  # type: ignore[arg-type]

    assert result.total_chains == 1
    pending = result.chains[0]
    assert pending.trigger_type == "business_change"
    assert pending.trigger_label == "Launch a structured note program"
    assert pending.redline_status == "PENDING"
    assert pending.decision is None
    assert pending.created_at is not None
    assert pending.change_date == business_change.change_date
    assert pending.rule == "FINRA Rule 3110 requires supervisory controls."
    assert pending.regulatory_citations[0].reference == "Rule 3110"
    assert pending.regulator_reference_policy_name == "FINRA Regulations"
    assert len(pending.gaps) == 2
    assert pending.gaps[0].summary == "Missing supervisory review cadence for structured notes."
    assert pending.gaps[1].rule == "FINRA Rule 2111"
    assert pending.gap_summary == "WSP should address structured note review."


def test_audit_trail_gap_filters_include_business_changes_or_material_gaps() -> None:
    high_business_change = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        business_change_id="change-1",
        trigger_type="business_change",
        trigger_id="change-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Material gap.",
        regulatory_citations=[],
    )
    info_business_change = GapAnalysis(
        id="gap-2",
        org_id="org_1",
        business_change_id="change-2",
        trigger_type="business_change",
        trigger_id="change-2",
        policy_id="policy-1",
        impact_score="INFO",
        gap_summary="No gap.",
        regulatory_citations=[],
    )
    publication_gap = GapAnalysis(
        id="gap-3",
        org_id="org_1",
        publication_id="pub-1",
        trigger_type="publication",
        trigger_id="pub-1",
        policy_id="policy-1",
        impact_score="HIGH",
        gap_summary="Publication gap.",
        regulatory_citations=[],
    )
    info_publication_gap = GapAnalysis(
        id="gap-4",
        org_id="org_1",
        publication_id="pub-2",
        trigger_type="publication",
        trigger_id="pub-2",
        policy_id="policy-1",
        impact_score="INFO",
        gap_summary="Informational publication gap.",
        regulatory_citations=[],
    )

    info_marketing_gap = GapAnalysis(
        id="gap-5",
        org_id="org_1",
        trigger_type="marketing_content",
        trigger_id="content-1",
        policy_id="policy-1",
        impact_score="INFO",
        gap_summary="Marketing homepage review.",
        regulatory_citations=[],
    )

    assert review_service.gap_analysis_visible_in_audit_trail(high_business_change)
    assert review_service.gap_analysis_visible_in_audit_trail(info_business_change)
    assert review_service.gap_analysis_visible_in_audit_trail(publication_gap)
    assert not review_service.gap_analysis_visible_in_audit_trail(info_publication_gap)
    assert review_service.gap_analysis_visible_in_audit_trail(info_marketing_gap)


def test_trigger_label_for_gap_uses_user_description_not_event_summary() -> None:
    gap = GapAnalysis(
        id="gap-1",
        org_id="org_1",
        policy_id="policy-1",
        business_change_id="change-1",
        trigger_type="business_change",
        trigger_id="change-1",
        impact_score="MEDIUM",
        gap_summary="AI-generated gap summary",
    )
    business_change = BusinessChange(
        id="change-1",
        org_id="org_1",
        change_type="new_product",
        description="User typed this description",
        event_summary="AI generated event summary that should not appear",
    )

    label = review_service.trigger_label_for_gap(gap, None, business_change)

    assert label == "User typed this description"
    assert "AI generated" not in label


def test_mark_approved_revisions_reuploaded_stamps_matching_policy() -> None:
    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="APPROVED",
        reviewed_at=datetime.utcnow() - timedelta(hours=3),
        reuploaded_at=None,
    )
    db = RecordingSession({PolicyRevision: [revision]})

    count = review_service.mark_approved_revisions_reuploaded(
        db,  # type: ignore[arg-type]
        org_id="org_1",
        policy_name="finra",
    )

    assert count == 1
    assert revision.reuploaded_at is not None
    assert db.committed is True


def test_organization_primary_email_prefers_cco() -> None:
    org = Organization(
        id="org-row",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "cco_email": "cco@example.com",
            "assigned_reviewer_email": "reviewer@example.com",
        },
    )
    assert review_service.organization_primary_email(org) == "cco@example.com"
    assert review_service.organization_primary_name(org) == "Compliance Team"


@pytest.mark.asyncio
async def test_check_approved_reupload_reminders_sends_daily_after_grace(monkeypatch) -> None:
    from app.workers import review_jobs

    revision = PolicyRevision(
        id="rev-1",
        gap_analysis_id="gap-1",
        policy_id="policy-1",
        org_id="org_1",
        status="APPROVED",
        reviewed_at=datetime.utcnow() - timedelta(hours=3),
        reuploaded_at=None,
        reupload_reminded_at=None,
    )
    policy = Policy(
        id="policy-1",
        org_id="org_1",
        name="FINRA",
        version=1,
        status="ACTIVE",
    )
    org = Organization(
        id="org-row",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={
            "cco_email": "cco@example.com",
            "cco_name": "Chief CCO",
        },
    )
    db = RecordingSession({(PolicyRevision, Organization, Policy): [(revision, org, policy)]})

    class SessionFactory:
        def __call__(self):
            return db

    monkeypatch.setattr(review_jobs, "SessionLocal", SessionFactory())
    monkeypatch.setattr(
        review_jobs,
        "get_settings",
        lambda: Settings(
            FRONTEND_URL="http://localhost:3000",
            REUPLOAD_REMINDER_GRACE_HOURS=2,
        ),
    )
    monkeypatch.setattr(
        review_jobs,
        "revision_reupload_notify_contact",
        lambda _db, _revision, _org: ("cco@example.com", "Chief CCO"),
    )
    mock_send = AsyncMock(return_value={"status": "sent"})
    monkeypatch.setattr(review_jobs, "send_reupload_reminder", mock_send)

    result = await review_jobs.check_approved_reupload_reminders()

    assert result["status"] == "completed"
    assert result["reminded_count"] == 1
    assert revision.reupload_reminded_at is not None
    mock_send.assert_awaited_once()
    assert mock_send.call_args.kwargs["policy_name"] == "FINRA"
    assert mock_send.call_args.kwargs["to_email"] == "cco@example.com"
    assert mock_send.call_args.kwargs["is_immediate"] is False
    assert "policies?upload=1&policy_name=FINRA" in mock_send.call_args.kwargs["upload_url"]


def test_review_decision_request_requires_notes_for_reject() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ReviewDecisionRequest(decision='REJECTED', notes=None)
    with pytest.raises(ValidationError):
        ReviewDecisionRequest(decision='APPROVED_WITH_EDITS', notes='   ')
    allowed = ReviewDecisionRequest(decision='APPROVED_ALL_CHANGES', notes=None)
    assert allowed.notes is None
    rejected = ReviewDecisionRequest(decision='REJECTED', notes='  Needs stronger CIP. ')
    assert rejected.notes == 'Needs stronger CIP.'
    assert ReviewDecisionRequest(decision='APPROVE_REVIEW', notes=None).notes is None
    assert ReviewDecisionRequest(decision='REJECT_REVIEW', notes=None).notes is None
    marketing = ReviewDecisionRequest(decision='PARTIALLY_ACCEPTED', notes='  Accept hero only. ')
    assert marketing.notes == 'Accept hero only.'


@pytest.mark.asyncio
async def test_marketing_review_decision_leaves_queue_without_policy_upload(monkeypatch) -> None:
    revision = PolicyRevision(
        id="rev-mkt",
        gap_analysis_id="gap-mkt",
        policy_id="policy-mkt",
        org_id="org_1",
        redline_file_url="storage://policy-redlines/org_1/policy-revisions/rev-mkt/redline.docx",
        status="PENDING",
        created_at=datetime.utcnow() - timedelta(days=1),
    )
    policy = Policy(
        id="policy-mkt",
        org_id="org_1",
        name="Altruist homepage",
        version=1,
        status="ACTIVE",
        type="MARKETING_CONTENT",
    )
    org = Organization(
        id="internal-org-id",
        auth_org_id="org_1",
        name="Test Org",
        approval_chain={},
    )
    gap = GapAnalysis(
        id="gap-mkt",
        org_id="org_1",
        trigger_type="marketing_content",
        trigger_id="content-1",
        policy_id="policy-mkt",
        impact_score="MEDIUM",
        gap_summary="Homepage review",
        retrieval_metadata={"redline_eligible": True, "marketing_content_id": "content-1"},
    )
    content = MarketingContent(
        id="content-1",
        org_id="org_1",
        name="Altruist homepage",
        status="reviewed",
    )
    db = RecordingSession(
        {
            PolicyRevision: revision,
            Policy: policy,
            Organization: org,
            GapAnalysis: gap,
            MarketingContent: content,
        }
    )
    monkeypatch.setattr(review_service, "send_decision_notification", AsyncMock(return_value={"status": "sent"}))
    monkeypatch.setattr(review_service, "send_reupload_reminder", AsyncMock())
    monkeypatch.setattr(review_service, "get_settings", lambda: Settings(FRONTEND_URL="http://localhost:3000"))
    monkeypatch.setattr(review_service, "organization_notification_recipients", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "app.services.compliance_history_service.record_history_from_gap_row",
        lambda *_args, **_kwargs: None,
    )

    response = await review_service.record_review_decision(
        "rev-mkt",
        ReviewDecisionRequest(decision="APPROVE_REVIEW", notes="Approved after CCO review."),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )

    assert response.status == "APPROVED"
    assert response.requires_policy_upload is False
    assert response.approval_mode == "REVIEW"
    assert revision.status == "APPROVED"
    assert revision.reuploaded_at is not None
    assert gap.retrieval_metadata["redline_eligible"] is False
    assert content.status == "approved"
    audit_logs = [item for item in db.added if isinstance(item, AuditLog)]
    assert audit_logs[0].metadata_json["marketing_review"] is True
    assert audit_logs[0].notes == "Approved after CCO review."

    revision.status = "PENDING"
    revision.reviewed_at = None
    revision.reuploaded_at = None
    gap.retrieval_metadata["redline_eligible"] = True
    partial = await review_service.record_review_decision(
        "rev-mkt",
        ReviewDecisionRequest(decision="PARTIALLY_ACCEPTED", notes="Accept disclosures, not the hero claim."),
        CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
        db,  # type: ignore[arg-type]
    )
    assert partial.status == "PARTIALLY_ACCEPTED"
    assert partial.requires_policy_upload is False
    assert revision.status == "PARTIALLY_ACCEPTED"

    from fastapi import HTTPException

    revision.status = "PENDING"
    revision.reviewed_at = None
    revision.reuploaded_at = None
    with pytest.raises(HTTPException) as caught:
        await review_service.record_review_decision(
            "rev-mkt",
            ReviewDecisionRequest(decision="APPROVED_WITH_EDITS", notes="No upload path."),
            CurrentUser(user_id="user-1", org_id="org_1", role="chief_compliance_officer"),
            db,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 400
    assert "partially accepted" in str(caught.value.detail).lower()


def test_pending_regulator_similarity_falls_back_to_gap_cosine() -> None:
    fields = review_service._pending_regulator_similarity_fields(
        {
            'regulator_document_similarity': [
                {
                    'regulator_source': 'ALL',
                    'policy_name': 'FINRA, SEC, and CFTC Regulations',
                    'cosine_similarity': 0.606,
                }
            ]
        }
    )
    assert fields['redline_regulator_cosine_similarity'] == pytest.approx(0.606)
    assert fields['redline_regulator_reference_policy_name'] == (
        'FINRA, SEC, and CFTC Regulations'
    )


def test_pending_regulator_similarity_falls_back_to_business_change_cosine() -> None:
    fields = review_service._pending_regulator_similarity_fields(
        {'business_change_policy_cosine_similarity': 0.734}
    )
    assert fields['redline_regulator_cosine_similarity'] == pytest.approx(0.734)


def test_pending_regulator_similarity_falls_back_to_marketing_cosine() -> None:
    fields = review_service._pending_regulator_similarity_fields(
        {
            'homepage_rule_similarity': {
                'rules': [
                    {'rule_id': '2210', 'cosine_similarity': 0.41, 'related': True},
                    {'rule_id': '204-2', 'cosine_similarity': 0.22, 'related': True},
                ]
            }
        }
    )
    assert fields['redline_regulator_cosine_similarity'] == pytest.approx(0.41)

