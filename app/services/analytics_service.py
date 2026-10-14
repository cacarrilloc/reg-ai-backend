from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.core.impact_score import normalize_impact_score
from app.core.time import to_utc, utc_now
from app.models.core import ComplianceHistory, GapAnalysis, Organization, Policy, PolicyRevision
from app.schemas.analytics import (
    AnalyticsDashboardResponse,
    AnalyticsInsights,
    AnalyticsKpis,
    ComplianceHistoryPolicyItem,
    ComplianceHistoryTimePoint,
    GapImpactTimePoint,
    PolicyCoverageItem,
    SimilarityHistogramBucket,
)
from app.services.document_chunk_service import (
    redline_internal_similarity_fields,
    redline_regulator_similarity_fields,
)

IMPACT_RANK = {'HIGH': 3, 'MEDIUM': 2, 'INFO': 1}
SIMILARITY_BUCKETS: list[tuple[str, float, float]] = [
    ('0.00–0.50', 0.0, 0.5),
    ('0.50–0.70', 0.5, 0.7),
    ('0.70–0.85', 0.7, 0.85),
    ('0.85–1.00', 0.85, 1.01),
]
GAP_HISTORY_WEEKS = 12
COMPLIANCE_HISTORY_TOP_POLICIES = 5
INTERNAL_POLICY_TYPE = 'REGULATORY_REFERENCE'
_ANALYTICS_CACHE_TTL_SECONDS = 60
_analytics_cache: dict[str, tuple[float, AnalyticsDashboardResponse]] = {}
_kpi_cache: dict[str, tuple[float, AnalyticsKpis]] = {}


@dataclass(frozen=True)
class _GapAnalyticsRow:
    gap_id: str
    impact: str
    created_at: datetime | None
    metadata: dict
    policy_name: str
    policy_type: str


@dataclass(frozen=True)
class _PendingReviewStats:
    pending_reviews: int = 0
    high_impact_pending: int = 0
    redlines_ready: int = 0
    overdue_reviews: int = 0


def _week_start(value: datetime) -> date:
    normalized = to_utc(value)
    return (normalized - timedelta(days=normalized.weekday())).date()


def _week_label(week_start: date) -> str:
    return week_start.strftime('%b %d')


def _worst_impact(scores: list[str]) -> str | None:
    if not scores:
        return None
    return max(scores, key=lambda score: IMPACT_RANK.get(score, 0))


def _policy_is_compliant(item: PolicyCoverageItem) -> bool:
    """A policy is compliant when all high- and medium-impact gaps are resolved."""
    return item.open_high_gaps == 0 and item.open_medium_gaps == 0


def _empty_histogram() -> list[SimilarityHistogramBucket]:
    return [
        SimilarityHistogramBucket(label=label, min_score=min_score, max_score=max_score)
        for label, min_score, max_score in SIMILARITY_BUCKETS
    ]


def _bucket_index(score: float) -> int:
    for index, (_label, min_score, max_score) in enumerate(SIMILARITY_BUCKETS):
        if min_score <= score < max_score:
            return index
    return len(SIMILARITY_BUCKETS) - 1


def _build_histogram(scores: list[float]) -> tuple[list[SimilarityHistogramBucket], float | None]:
    buckets = _empty_histogram()
    if not scores:
        return buckets, None
    for score in scores:
        buckets[_bucket_index(score)].count += 1
    average = sum(scores) / len(scores)
    return buckets, average


def _approved_gap_ids(db: Session, org_id: str) -> set[str]:
    rows = (
        db.query(PolicyRevision.gap_analysis_id)
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
        )
        .all()
    )
    return {row[0] for row in rows}


def _pending_review_stats(db: Session, org_id: str) -> _PendingReviewStats:
    now = utc_now()
    rows = (
        db.query(
            PolicyRevision.redline_file_url,
            PolicyRevision.review_requested_at,
            PolicyRevision.created_at,
            GapAnalysis.impact_score,
            Organization.escalation_days,
        )
        .join(GapAnalysis, GapAnalysis.id == PolicyRevision.gap_analysis_id)
        .outerjoin(Organization, Organization.auth_org_id == PolicyRevision.org_id)
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'PENDING',
            PolicyRevision.review_requested_at.is_not(None),
            GapAnalysis.org_id == org_id,
        )
        .all()
    )
    pending_reviews = len(rows)
    high_impact_pending = 0
    redlines_ready = 0
    overdue_reviews = 0
    for redline_file_url, review_requested_at, created_at, impact_score, escalation_days in rows:
        if normalize_impact_score(impact_score) == 'HIGH':
            high_impact_pending += 1
        if redline_file_url:
            redlines_ready += 1
        days = escalation_days if escalation_days else 7
        anchor = review_requested_at or created_at
        if anchor and to_utc(anchor) <= now - timedelta(days=days):
            overdue_reviews += 1
    return _PendingReviewStats(
        pending_reviews=pending_reviews,
        high_impact_pending=high_impact_pending,
        redlines_ready=redlines_ready,
        overdue_reviews=overdue_reviews,
    )


def _active_internal_policies(db: Session, org_id: str) -> list[Policy]:
    policies = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != INTERNAL_POLICY_TYPE,
        )
        .order_by(Policy.name.asc(), desc(Policy.version))
        .all()
    )
    latest_by_name: dict[str, Policy] = {}
    for policy in policies:
        if policy.name not in latest_by_name:
            latest_by_name[policy.name] = policy
    return sorted(latest_by_name.values(), key=lambda policy: policy.name.lower())


def _load_gap_rows(db: Session, org_id: str) -> list[_GapAnalyticsRow]:
    rows = (
        db.query(
            GapAnalysis.id,
            GapAnalysis.impact_score,
            GapAnalysis.created_at,
            GapAnalysis.retrieval_metadata,
            Policy.name,
            Policy.type,
        )
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .filter(GapAnalysis.org_id == org_id, Policy.org_id == org_id)
        .all()
    )
    return [
        _GapAnalyticsRow(
            gap_id=gap_id,
            impact=normalize_impact_score(impact_score),
            created_at=created_at,
            metadata=metadata or {},
            policy_name=policy_name,
            policy_type=policy_type,
        )
        for gap_id, impact_score, created_at, metadata, policy_name, policy_type in rows
    ]


def _open_gaps_by_policy_name(
    gap_rows: list[_GapAnalyticsRow],
    approved_gap_ids: set[str],
) -> dict[str, dict[str, list[str]]]:
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in gap_rows:
        if row.policy_type == INTERNAL_POLICY_TYPE:
            continue
        if row.gap_id in approved_gap_ids:
            continue
        grouped[row.policy_name][row.impact].append(row.gap_id)
    return grouped


def _gaps_by_impact_over_time(
    gap_rows: list[_GapAnalyticsRow],
    *,
    earliest: datetime,
) -> list[GapImpactTimePoint]:
    earliest_utc = to_utc(earliest)
    now = utc_now()
    current_week = _week_start(now)
    week_starts = [
        current_week - timedelta(weeks=offset)
        for offset in range(GAP_HISTORY_WEEKS - 1, -1, -1)
    ]
    counts: dict[date, dict[str, int]] = {
        week_start: {'HIGH': 0, 'MEDIUM': 0, 'INFO': 0}
        for week_start in week_starts
    }
    for row in gap_rows:
        if row.created_at is None or to_utc(row.created_at) < earliest_utc:
            continue
        week = _week_start(row.created_at)
        if week not in counts:
            continue
        counts[week][row.impact] += 1

    return [
        GapImpactTimePoint(
            period_start=week_start,
            period_label=_week_label(week_start),
            high=counts[week_start]['HIGH'],
            medium=counts[week_start]['MEDIUM'],
            info=counts[week_start]['INFO'],
        )
        for week_start in week_starts
    ]


def _policy_coverage(
    internal_policies: list[Policy],
    open_gaps_by_name: dict[str, dict[str, list[str]]],
) -> list[PolicyCoverageItem]:
    coverage: list[PolicyCoverageItem] = []
    for policy in internal_policies:
        impact_groups = open_gaps_by_name.get(policy.name, {})
        open_high = len(impact_groups.get('HIGH', []))
        open_medium = len(impact_groups.get('MEDIUM', []))
        open_impacts = [
            impact
            for impact, gap_ids in impact_groups.items()
            if gap_ids
        ]
        coverage.append(
            PolicyCoverageItem(
                policy_id=policy.id,
                policy_name=policy.name,
                open_high_gaps=open_high,
                open_medium_gaps=open_medium,
                worst_impact=_worst_impact(open_impacts),
            )
        )
    coverage.sort(
        key=lambda item: (
            -IMPACT_RANK.get(item.worst_impact or '', 0),
            -item.open_high_gaps,
            item.policy_name.lower(),
        )
    )
    return coverage


def _compliance_history_over_time(
    rows: list[tuple[datetime | None, str]],
    *,
    earliest: datetime,
) -> list[ComplianceHistoryTimePoint]:
    earliest_utc = to_utc(earliest)
    now = utc_now()
    current_week = _week_start(now)
    week_starts = [
        current_week - timedelta(weeks=offset)
        for offset in range(GAP_HISTORY_WEEKS - 1, -1, -1)
    ]
    counts: dict[date, int] = {week_start: 0 for week_start in week_starts}
    for incident_at, _policy_name in rows:
        if incident_at is None or to_utc(incident_at) < earliest_utc:
            continue
        week = _week_start(incident_at)
        if week in counts:
            counts[week] += 1

    return [
        ComplianceHistoryTimePoint(
            period_start=week_start,
            period_label=_week_label(week_start),
            incidents=counts[week_start],
        )
        for week_start in week_starts
    ]


def _compliance_history_top_policies(
    rows: list[tuple[datetime | None, str]],
    *,
    earliest: datetime,
    limit: int = COMPLIANCE_HISTORY_TOP_POLICIES,
) -> list[ComplianceHistoryPolicyItem]:
    earliest_utc = to_utc(earliest)
    tallies: dict[str, int] = defaultdict(int)
    for incident_at, policy_name in rows:
        if incident_at is None or to_utc(incident_at) < earliest_utc:
            continue
        name = (policy_name or '').strip() or 'Untitled policy'
        tallies[name] += 1

    ranked = sorted(
        tallies.items(),
        key=lambda item: (-item[1], item[0].lower()),
    )
    return [
        ComplianceHistoryPolicyItem(policy_name=name, incident_count=count)
        for name, count in ranked[:limit]
    ]


def _load_compliance_history_rows(
    db: Session,
    org_id: str,
    *,
    earliest: datetime,
) -> list[tuple[datetime | None, str]]:
    return (
        db.query(ComplianceHistory.incident_at, ComplianceHistory.policy_name)
        .filter(
            ComplianceHistory.org_id == org_id,
            ComplianceHistory.incident_at >= earliest,
        )
        .all()
    )


def _collect_similarity_scores(gap_rows: list[_GapAnalyticsRow]) -> tuple[list[float], list[float]]:
    internal_scores: list[float] = []
    regulator_scores: list[float] = []
    for row in gap_rows:
        internal = redline_internal_similarity_fields(row.metadata).get('redline_internal_cosine_similarity')
        regulator = redline_regulator_similarity_fields(row.metadata).get('redline_regulator_cosine_similarity')
        if internal is not None:
            internal_scores.append(float(internal))
        if regulator is not None:
            regulator_scores.append(float(regulator))
    return internal_scores, regulator_scores


def get_analytics_dashboard(db: Session, org_id: str) -> AnalyticsDashboardResponse:
    cached = _analytics_cache.get(org_id)
    now = time.monotonic()
    if cached and now - cached[0] < _ANALYTICS_CACHE_TTL_SECONDS:
        return cached[1]

    response = _build_analytics_dashboard(db, org_id)
    _analytics_cache[org_id] = (now, response)
    return response


def get_dashboard_kpis(db: Session, org_id: str) -> AnalyticsKpis:
    """Load only dashboard KPI data, avoiding the full Insights analytics scan."""
    cached = _kpi_cache.get(org_id)
    now = time.monotonic()
    if cached and now - cached[0] < _ANALYTICS_CACHE_TTL_SECONDS:
        return cached[1]

    pending_stats = _pending_review_stats(db, org_id)
    month_start = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    approved_this_month = (
        db.query(func.count(PolicyRevision.id))
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
            PolicyRevision.reviewed_at.is_not(None),
            PolicyRevision.reviewed_at >= month_start,
        )
        .scalar()
        or 0
    )
    approved_revision_exists = (
        db.query(PolicyRevision.id)
        .filter(
            PolicyRevision.gap_analysis_id == GapAnalysis.id,
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
        )
        .exists()
    )
    open_high_gaps = (
        db.query(func.count(GapAnalysis.id))
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .filter(
            GapAnalysis.org_id == org_id,
            GapAnalysis.impact_score == 'HIGH',
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != INTERNAL_POLICY_TYPE,
            ~approved_revision_exists,
        )
        .scalar()
        or 0
    )
    response = AnalyticsKpis(
        pending_reviews=pending_stats.pending_reviews,
        high_impact_pending=pending_stats.high_impact_pending,
        redlines_ready=pending_stats.redlines_ready,
        overdue_reviews=pending_stats.overdue_reviews,
        approved_this_month=int(approved_this_month),
        open_high_gaps=int(open_high_gaps),
    )
    _kpi_cache[org_id] = (now, response)
    return response


def _build_analytics_dashboard(db: Session, org_id: str) -> AnalyticsDashboardResponse:
    approved_gap_ids = _approved_gap_ids(db, org_id)
    pending_stats = _pending_review_stats(db, org_id)
    month_start = utc_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    approved_this_month = (
        db.query(func.count(PolicyRevision.id))
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
            PolicyRevision.reviewed_at.is_not(None),
            PolicyRevision.reviewed_at >= month_start,
        )
        .scalar()
        or 0
    )

    internal_policies = _active_internal_policies(db, org_id)
    gap_rows = _load_gap_rows(db, org_id)
    earliest_history = to_utc(
        datetime.combine(
            _week_start(utc_now()) - timedelta(weeks=GAP_HISTORY_WEEKS - 1),
            datetime.min.time(),
        )
    )
    open_gaps_by_name = _open_gaps_by_policy_name(gap_rows, approved_gap_ids)
    policy_coverage = _policy_coverage(internal_policies, open_gaps_by_name)

    open_high_gaps = sum(item.open_high_gaps for item in policy_coverage)
    open_medium_gaps = sum(item.open_medium_gaps for item in policy_coverage)
    compliant_policies = sum(
        1 for item in policy_coverage if _policy_is_compliant(item)
    )
    total_internal = len(internal_policies)
    compliance_score = (
        round((compliant_policies / total_internal) * 100, 1) if total_internal else 0.0
    )

    internal_scores, regulator_scores = _collect_similarity_scores(gap_rows)
    internal_histogram, internal_average = _build_histogram(internal_scores)
    regulator_histogram, regulator_average = _build_histogram(regulator_scores)
    history_rows = _load_compliance_history_rows(db, org_id, earliest=earliest_history)

    return AnalyticsDashboardResponse(
        kpis=AnalyticsKpis(
            pending_reviews=pending_stats.pending_reviews,
            high_impact_pending=pending_stats.high_impact_pending,
            redlines_ready=pending_stats.redlines_ready,
            overdue_reviews=pending_stats.overdue_reviews,
            approved_this_month=approved_this_month,
            open_high_gaps=open_high_gaps,
        ),
        gaps_by_impact_over_time=_gaps_by_impact_over_time(gap_rows, earliest=earliest_history),
        policy_coverage=policy_coverage,
        compliance_history_over_time=_compliance_history_over_time(
            history_rows,
            earliest=earliest_history,
        ),
        compliance_history_top_policies=_compliance_history_top_policies(
            history_rows,
            earliest=earliest_history,
        ),
        insights=AnalyticsInsights(
            compliance_score=compliance_score,
            compliant_policies=compliant_policies,
            total_internal_policies=total_internal,
            open_high_gaps=open_high_gaps,
            open_medium_gaps=open_medium_gaps,
            redline_internal_histogram=internal_histogram,
            redline_regulator_histogram=regulator_histogram,
            average_redline_internal_similarity=internal_average,
            average_redline_regulator_similarity=regulator_average,
            redline_internal_sample_size=len(internal_scores),
            redline_regulator_sample_size=len(regulator_scores),
        ),
    )
