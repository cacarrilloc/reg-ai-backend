from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.schemas.analytics import PolicyCoverageItem
from app.services.analytics_service import (
    GAP_HISTORY_WEEKS,
    _build_histogram,
    _compliance_history_over_time,
    _compliance_history_top_policies,
    _GapAnalyticsRow,
    _gaps_by_impact_over_time,
    _policy_coverage,
    _policy_is_compliant,
    _worst_impact,
)


def test_worst_impact_prefers_high() -> None:
    assert _worst_impact(['MEDIUM', 'HIGH', 'INFO']) == 'HIGH'
    assert _worst_impact([]) is None


def test_build_histogram_buckets_scores() -> None:
    buckets, average = _build_histogram([0.42, 0.55, 0.91, 0.78])
    assert average is not None
    assert round(average, 2) == 0.67
    assert buckets[0].count == 1
    assert buckets[1].count == 1
    assert buckets[2].count == 1
    assert buckets[3].count == 1


def test_build_histogram_returns_empty_average_for_no_scores() -> None:
    buckets, average = _build_histogram([])
    assert average is None
    assert all(bucket.count == 0 for bucket in buckets)


def test_policy_coverage_sorts_by_severity() -> None:
    policies = [
        SimpleNamespace(id='policy-a', name='Alpha'),
        SimpleNamespace(id='policy-b', name='Beta'),
    ]
    open_gaps = {
        'Alpha': {'INFO': ['gap-1']},
        'Beta': {'HIGH': ['gap-2'], 'MEDIUM': ['gap-3']},
    }
    coverage = _policy_coverage(policies, open_gaps)
    assert coverage[0].policy_name == 'Beta'
    assert coverage[0].open_high_gaps == 1
    assert coverage[1].policy_name == 'Alpha'
    assert coverage[1].worst_impact == 'INFO'


def test_policy_coverage_marks_compliant_policies() -> None:
    policies = [SimpleNamespace(id='policy-a', name='Alpha')]
    coverage = _policy_coverage(policies, {})
    assert coverage[0].open_high_gaps == 0
    assert coverage[0].worst_impact is None


def test_policy_is_compliant_requires_no_high_or_medium_gaps() -> None:
    assert _policy_is_compliant(
        PolicyCoverageItem(
            policy_id='p1',
            policy_name='Clean',
            open_high_gaps=0,
            open_medium_gaps=0,
        )
    )
    assert not _policy_is_compliant(
        PolicyCoverageItem(
            policy_id='p2',
            policy_name='Medium only',
            open_high_gaps=0,
            open_medium_gaps=1,
        )
    )
    assert not _policy_is_compliant(
        PolicyCoverageItem(
            policy_id='p3',
            policy_name='High only',
            open_high_gaps=1,
            open_medium_gaps=0,
        )
    )


def test_compliance_score_excludes_medium_gap_policies() -> None:
    policies = [
        SimpleNamespace(id='policy-a', name='Alpha'),
        SimpleNamespace(id='policy-b', name='Beta'),
    ]
    open_gaps = {
        'Alpha': {'HIGH': ['gap-1']},
        'Beta': {'MEDIUM': ['gap-2']},
    }
    coverage = _policy_coverage(policies, open_gaps)
    compliant = sum(1 for item in coverage if _policy_is_compliant(item))
    assert compliant == 0
    assert round((compliant / len(coverage)) * 100, 1) == 0.0


def test_gaps_by_impact_over_time_handles_aware_created_at() -> None:
    now = datetime.now(UTC)
    earliest = now - timedelta(weeks=GAP_HISTORY_WEEKS)
    gap_rows = [
        _GapAnalyticsRow(
            gap_id='gap-1',
            impact='HIGH',
            created_at=now - timedelta(days=2),
            metadata={},
            policy_name='WSP',
            policy_type='uploaded',
        )
    ]
    points = _gaps_by_impact_over_time(gap_rows, earliest=earliest)
    assert len(points) == GAP_HISTORY_WEEKS
    assert sum(point.high for point in points) == 1


def test_compliance_history_over_time_counts_weekly_incidents() -> None:
    now = datetime.now(UTC)
    earliest = now - timedelta(weeks=GAP_HISTORY_WEEKS)
    rows = [
        (now - timedelta(days=1), 'AML Policy'),
        (now - timedelta(days=2), 'AML Policy'),
        (now - timedelta(days=10), 'WSP'),
        (now - timedelta(weeks=20), 'Old Policy'),
    ]
    points = _compliance_history_over_time(rows, earliest=earliest)
    assert len(points) == GAP_HISTORY_WEEKS
    assert sum(point.incidents for point in points) == 3


def test_compliance_history_top_policies_ranks_by_incident_count() -> None:
    now = datetime.now(UTC)
    earliest = now - timedelta(weeks=GAP_HISTORY_WEEKS)
    rows = [
        (now - timedelta(days=1), 'AML Policy'),
        (now - timedelta(days=2), 'AML Policy'),
        (now - timedelta(days=3), 'WSP'),
        (now - timedelta(days=4), 'KYC Policy'),
        (now - timedelta(days=5), 'KYC Policy'),
        (now - timedelta(days=6), 'KYC Policy'),
    ]
    top = _compliance_history_top_policies(rows, earliest=earliest, limit=2)
    assert [item.policy_name for item in top] == ['KYC Policy', 'AML Policy']
    assert top[0].incident_count == 3
    assert top[1].incident_count == 2
