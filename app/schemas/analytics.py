from datetime import date

from pydantic import BaseModel, Field


class AnalyticsKpis(BaseModel):
    pending_reviews: int = 0
    high_impact_pending: int = 0
    redlines_ready: int = 0
    overdue_reviews: int = 0
    approved_this_month: int = 0
    open_high_gaps: int = 0


class GapImpactTimePoint(BaseModel):
    period_start: date
    period_label: str
    high: int = 0
    medium: int = 0
    info: int = 0


class PolicyCoverageItem(BaseModel):
    policy_id: str
    policy_name: str
    open_high_gaps: int = 0
    open_medium_gaps: int = 0
    worst_impact: str | None = None


class SimilarityHistogramBucket(BaseModel):
    label: str
    min_score: float
    max_score: float
    count: int = 0


class ComplianceHistoryTimePoint(BaseModel):
    period_start: date
    period_label: str
    incidents: int = 0


class ComplianceHistoryPolicyItem(BaseModel):
    policy_name: str
    incident_count: int = 0


class AnalyticsInsights(BaseModel):
    compliance_score: float = 0.0
    compliant_policies: int = 0
    total_internal_policies: int = 0
    open_high_gaps: int = 0
    open_medium_gaps: int = 0
    redline_internal_histogram: list[SimilarityHistogramBucket] = Field(default_factory=list)
    redline_regulator_histogram: list[SimilarityHistogramBucket] = Field(default_factory=list)
    average_redline_internal_similarity: float | None = None
    average_redline_regulator_similarity: float | None = None
    redline_internal_sample_size: int = 0
    redline_regulator_sample_size: int = 0


class AnalyticsDashboardResponse(BaseModel):
    kpis: AnalyticsKpis
    gaps_by_impact_over_time: list[GapImpactTimePoint] = Field(default_factory=list)
    policy_coverage: list[PolicyCoverageItem] = Field(default_factory=list)
    compliance_history_over_time: list[ComplianceHistoryTimePoint] = Field(default_factory=list)
    compliance_history_top_policies: list[ComplianceHistoryPolicyItem] = Field(
        default_factory=list
    )
    insights: AnalyticsInsights
