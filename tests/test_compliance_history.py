"""Tests for compliance history metadata recording and listing."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.core import ComplianceHistory, GapAnalysis, Policy
from app.services import compliance_history_service as history_service


class _Query:
    def __init__(self, result: object | None = None, results: list[object] | None = None) -> None:
        self._result = result
        self._results = results or []

    def filter(self, *_args: object, **_kwargs: object) -> _Query:
        return self

    def outerjoin(self, *_args: object, **_kwargs: object) -> _Query:
        return self

    def order_by(self, *_args: object, **_kwargs: object) -> _Query:
        return self

    def limit(self, *_args: object, **_kwargs: object) -> _Query:
        return self

    def one_or_none(self) -> object | None:
        if self._result is not None:
            return self._result
        if self._results:
            return self._results[0]
        return None

    def all(self) -> list[object]:
        return list(self._results)


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self._by_model: dict[type, object | None] = {}
        self._list_by_model: dict[type, list[object]] = {}

    def set_one(self, model: type, value: object | None) -> None:
        self._by_model[model] = value

    def set_list(self, model: type, values: list[object]) -> None:
        self._list_by_model[model] = values

    def query(self, model: type) -> _Query:
        if model in self._list_by_model:
            return _Query(results=self._list_by_model[model])
        return _Query(result=self._by_model.get(model))

    def add(self, obj: object) -> None:
        self.added.append(obj)


def test_gap_finding_rule_texts_from_sentence_findings() -> None:
    labels = history_service.gap_finding_rule_texts(
        rule='FINRA Rule 3110(a)-(b) (Supervision – supervisory system)',
        retrieval_metadata={
            'sentence_findings': [
                {
                    'rule': 'FINRA Rule 3120 (Supervisory Control System)',
                    'gap': 'Missing principal designation.',
                    'fix': 'Add principal designation.',
                },
                {
                    'rule': (
                        'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30) and '
                        'SEC Rule 10 (Incident Reporting for Covered Entities)'
                    ),
                    'gap': 'Missing incident evaluation.',
                    'fix': 'Add incident evaluation.',
                },
            ]
        },
    )
    assert labels == [
        'FINRA Rule 3120 (Supervisory Control System)',
        (
            'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30) and '
            'SEC Rule 10 (Incident Reporting for Covered Entities)'
        ),
    ]


def test_gap_finding_rule_texts_falls_back_to_top_level_rule() -> None:
    assert history_service.gap_finding_rule_texts(
        rule='FINRA Rule 3110',
        retrieval_metadata={},
    ) == ['FINRA Rule 3110']


def test_regulatory_rule_labels_from_sentence_findings() -> None:
    labels = history_service.regulatory_rule_labels(
        rule='FINRA Rule 3110(a)-(b) (Supervision – supervisory system)',
        citations=[
            {
                'source': 'Policy library',
                'reference': 'Policy upload VRM Test Policy v1',
            },
            {
                'source': 'Business change',
                'reference': 'Business change bd83ba54-488c-4807-bc71-615b56f54f65 (NEW_HIRE)',
            },
        ],
        retrieval_metadata={
            'sentence_findings': [
                {
                    'rule': 'FINRA Rule 3120 (Supervisory Control System)',
                    'gap': 'Missing principal designation.',
                    'fix': 'Add principal designation.',
                },
                {
                    'rule': (
                        'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30) and '
                        'SEC Rule 10 (Incident Reporting for Covered Entities)'
                    ),
                    'gap': 'Missing incident evaluation.',
                    'fix': 'Add incident evaluation.',
                },
            ]
        },
    )
    assert labels == [
        'FINRA Rule 3120',
        'SEC Regulation S-P',
        '17 CFR 248.30',
        'SEC Rule 10',
        'FINRA Rule 3110(a)-(b)',
    ]


def test_compact_regulation_labels_expands_multi_rule_phrases() -> None:
    assert history_service.compact_regulation_labels(
        'Under FINRA Rules 3110/3120 and BSA requirements, and SEC Rules 17a-3/17a-4.'
    ) == [
        'FINRA Rule 3110',
        'FINRA Rule 3120',
        'BSA',
        'SEC Rule 17a-3',
        'SEC Rule 17a-4',
    ]


def test_compact_regulation_labels_strips_prose_and_trigger_noise() -> None:
    assert history_service.compact_regulation_labels(
        'Policy library: Policy upload VRM Test Policy v1; FINRA Rule 3110(a)-(b) '
        '(Supervision – supervisory system and written supervisory procedures)'
    ) == ['FINRA Rule 3110(a)-(b)']
    assert history_service.compact_regulation_labels(
        'Business change bd83ba54-488c-4807-bc71-615b56f54f65 (NEW_HIRE)'
    ) == []
    assert history_service.compact_regulation_labels(
        'FINRA Rule 3110 requires written supervisory procedures.'
    ) == ['FINRA Rule 3110']
    assert history_service.compact_regulation_labels(
        'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30)'
    ) == ['SEC Regulation S-P', '17 CFR 248.30']


def test_list_joins_finding_rules_like_pending_approvals() -> None:
    now = datetime.utcnow()
    history = ComplianceHistory(
        id='history-new',
        org_id='org_example',
        gap_analysis_id='gap-new',
        policy_id='policy-1',
        policy_name='VRM Test Policy',
        regulatory_rules=[],
        trigger_type='policy_update',
        incident_at=now - timedelta(days=2),
    )
    gap = GapAnalysis(
        id='gap-new',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='MEDIUM',
        gap_summary='Multiple findings',
        rule=None,
        regulatory_citations=[],
        retrieval_metadata={
            'sentence_findings': [
                {'rule': 'FINRA Rule 3120 (Supervisory Control System)', 'gap': 'a', 'fix': 'b'},
                {
                    'rule': 'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30)',
                    'gap': 'c',
                    'fix': 'd',
                },
            ]
        },
        analysis_mode='LLM',
        created_at=now - timedelta(days=2),
    )
    db = _Session()
    db.set_list(ComplianceHistory, [history])
    db.set_list(GapAnalysis, [gap])

    response = history_service.list_compliance_history_for_org(
        db,  # type: ignore[arg-type]
        'org_example',
        days=30,
        limit=50,
    )

    assert response.total == 1
    assert response.items[0].regulatory_rules == [
        'FINRA Rule 3120 (Supervisory Control System); '
        'SEC Regulation S-P (Safeguards Rule, 17 CFR 248.30)',
    ]
    assert response.items[0].policy_name == 'VRM Test Policy'
    assert response.items[0].trigger_type == 'policy_update'


def test_list_uses_pending_approval_marketing_rule_labels() -> None:
    now = datetime.utcnow()
    history = ComplianceHistory(
        id='history-mkt',
        org_id='org_example',
        gap_analysis_id='gap-mkt',
        policy_id='policy-mkt',
        policy_name='Presentation: Reg-AI Pitch Deck',
        regulatory_rules=['206(4)-1'],
        trigger_type='marketing_content',
        incident_at=now - timedelta(days=1),
    )
    gap = GapAnalysis(
        id='gap-mkt',
        org_id='org_example',
        trigger_type='marketing_content',
        trigger_id='content-1',
        policy_id='policy-mkt',
        impact_score='HIGH',
        gap_summary='Pitch deck findings',
        rule='206(4)-1',
        regulatory_citations=[],
        retrieval_metadata={
            'marketing_format': 'presentation',
            'marketing_rule_codes': ['206(4)-1', '204-2', '2210', '2211-2216'],
            'sentence_findings': [
                {
                    'rule': '206(4)-1',
                    'gap': 'Missing performance disclosure.',
                    'fix': 'Add net-of-fees performance.',
                }
            ],
        },
        analysis_mode='MARKETING_REVIEW',
        created_at=now - timedelta(days=1),
    )
    db = _Session()
    db.set_list(ComplianceHistory, [history])
    db.set_list(GapAnalysis, [gap])

    response = history_service.list_compliance_history_for_org(
        db,  # type: ignore[arg-type]
        'org_example',
        days=30,
        limit=50,
    )

    assert response.total == 1
    assert response.items[0].regulatory_rules == [
        'SEC: Rule 206(4)-1',
        'SEC: Rule 204-2',
        'FINRA: Rule 2210',
        'FINRA: Rules 2211–2216',
    ]


def test_list_can_return_only_marketing_review_history() -> None:
    now = datetime.utcnow()
    marketing_history = ComplianceHistory(
        id='history-mkt',
        org_id='org_example',
        gap_analysis_id='gap-mkt',
        policy_id='policy-mkt',
        policy_name='Presentation — Reg-AI Pitch Deck',
        regulatory_rules=['SEC: Rule 206(4)-1'],
        trigger_type='marketing_content',
        incident_at=now - timedelta(days=1),
    )
    policy_history = ComplianceHistory(
        id='history-policy',
        org_id='org_example',
        gap_analysis_id='gap-policy',
        policy_id='policy-1',
        policy_name='VRM Test Policy',
        regulatory_rules=['FINRA Rule 3110'],
        trigger_type='policy_update',
        incident_at=now - timedelta(hours=1),
    )
    marketing_gap = GapAnalysis(
        id='gap-mkt',
        org_id='org_example',
        trigger_type='marketing_content',
        trigger_id='content-1',
        policy_id='policy-mkt',
        impact_score='HIGH',
        gap_summary='Pitch deck findings',
        rule='206(4)-1',
        regulatory_citations=[],
        retrieval_metadata={
            'marketing_rule_codes': ['206(4)-1'],
            'sentence_findings': [
                {'rule': '206(4)-1', 'gap': 'Missing disclosure.', 'fix': 'Add net performance.'}
            ],
        },
        analysis_mode='MARKETING_REVIEW',
        created_at=now - timedelta(days=1),
    )
    policy_gap = GapAnalysis(
        id='gap-policy',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='MEDIUM',
        gap_summary='Policy gap',
        rule='FINRA Rule 3110',
        regulatory_citations=[],
        retrieval_metadata={},
        analysis_mode='LLM',
        created_at=now - timedelta(hours=1),
    )
    db = _Session()
    db.set_list(ComplianceHistory, [policy_history, marketing_history])
    db.set_list(GapAnalysis, [policy_gap, marketing_gap])

    response = history_service.list_compliance_history_for_org(
        db,  # type: ignore[arg-type]
        'org_example',
        days=30,
        limit=50,
        trigger_type='marketing_content',
    )

    assert response.total == 1
    assert response.items[0].trigger_type == 'marketing_content'
    assert response.items[0].policy_name == 'Presentation: Reg-AI Pitch Deck'


def test_list_prefers_gap_trigger_type_over_stale_history_column() -> None:
    """Trigger badges must mirror gap_analyses.trigger_type across all tables."""
    now = datetime.utcnow()
    history = ComplianceHistory(
        id='history-stale',
        org_id='org_example',
        gap_analysis_id='gap-stale',
        policy_id='policy-1',
        policy_name='VRM Test Policy',
        regulatory_rules=['FINRA Rule 3110'],
        trigger_type='policy_update',  # stale denormalized copy
        incident_at=now - timedelta(days=1),
    )
    gap = GapAnalysis(
        id='gap-stale',
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='bc-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Business change gap',
        rule='FINRA Rule 3110',
        regulatory_citations=[],
        retrieval_metadata={},
        analysis_mode='LLM',
        created_at=now - timedelta(days=1),
    )
    db = _Session()
    db.set_list(ComplianceHistory, [history])
    db.set_list(GapAnalysis, [gap])

    response = history_service.list_compliance_history_for_org(
        db,  # type: ignore[arg-type]
        'org_example',
        days=30,
        limit=50,
    )

    assert response.total == 1
    assert response.items[0].trigger_type == 'business_change'


def test_upsert_compliance_history_inserts_new_row() -> None:
    db = _Session()
    db.set_one(ComplianceHistory, None)
    db.set_one(
        Policy,
        Policy(
            id='policy-1',
            org_id='org_example',
            name='AML Policy',
            type='uploaded',
            version=1,
            status='ACTIVE',
        ),
    )

    history_service.upsert_compliance_history_for_gap(
        db,  # type: ignore[arg-type]
        org_id='org_example',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        rule='FINRA Rule 3310',
        citations=[{'source': 'FINRA', 'reference': 'Rule 3310'}],
        incident_at=datetime(2026, 7, 1, 12, 0, 0),
        trigger_type='policy_update',
    )

    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, ComplianceHistory)
    assert row.policy_name == 'AML Policy'
    assert row.regulatory_rules == ['FINRA Rule 3310']
    assert row.trigger_type == 'policy_update'
    assert row.incident_at == datetime(2026, 7, 1, 12, 0, 0)


def test_upsert_compliance_history_updates_existing_without_changing_incident_at() -> None:
    existing = ComplianceHistory(
        id='history-1',
        org_id='org_example',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        policy_name='Old Name',
        regulatory_rules=['Old rule'],
        incident_at=datetime(2026, 6, 1, 9, 0, 0),
    )
    db = _Session()
    db.set_one(ComplianceHistory, existing)
    db.set_one(
        Policy,
        Policy(
            id='policy-1',
            org_id='org_example',
            name='Updated AML Policy',
            type='uploaded',
            version=2,
            status='ACTIVE',
        ),
    )

    history_service.upsert_compliance_history_for_gap(
        db,  # type: ignore[arg-type]
        org_id='org_example',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        rule='FINRA Rule 3110',
        citations=[],
        incident_at=datetime(2026, 7, 17, 12, 0, 0),
        trigger_type='business_change',
    )

    assert db.added == []
    assert existing.policy_name == 'Updated AML Policy'
    assert existing.regulatory_rules == ['FINRA Rule 3110']
    assert existing.trigger_type == 'business_change'
    assert existing.incident_at == datetime(2026, 6, 1, 9, 0, 0)


def test_safe_upsert_swallows_errors(monkeypatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('db down')

    monkeypatch.setattr(history_service, 'upsert_compliance_history_for_gap', boom)
    history_service.safe_upsert_compliance_history_for_gap(
        SimpleNamespace(),  # type: ignore[arg-type]
        org_id='org_example',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        rule=None,
        citations=[],
    )


def test_record_history_from_gap_row_uses_gap_fields(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture(db: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(history_service, 'safe_upsert_compliance_history_for_gap', capture)
    gap = GapAnalysis(
        id='gap-1',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Summary',
        rule='FINRA Rule 3110',
        regulatory_citations=[{'source': 'FINRA', 'reference': 'Rule 3110'}],
        retrieval_metadata={'sentence_findings': [{'rule': 'FINRA Rule 3120', 'gap': 'x', 'fix': 'y'}]},
        analysis_mode='LLM',
        created_at=datetime(2026, 7, 10, 8, 0, 0),
    )
    history_service.record_history_from_gap_row(SimpleNamespace(), gap)  # type: ignore[arg-type]
    assert captured['gap_analysis_id'] == 'gap-1'
    assert captured['rule'] == 'FINRA Rule 3110'
    assert captured['trigger_type'] == 'policy_update'
    assert captured['retrieval_metadata'] == gap.retrieval_metadata
    assert captured['incident_at'] == datetime(2026, 7, 10, 8, 0, 0)


def test_backfill_missing_compliance_history_writes_for_gaps_without_history() -> None:
    gap = GapAnalysis(
        id='gap-missing-history',
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='bc-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Business change gap',
        rule='FINRA Rule 3110',
        regulatory_citations=[],
        retrieval_metadata={},
        analysis_mode='LLM',
        created_at=datetime(2026, 7, 1, 9, 0, 0),
    )
    db = _Session()
    db.set_list(GapAnalysis, [gap])
    db.set_one(ComplianceHistory, None)
    db.set_one(
        Policy,
        Policy(
            id='policy-1',
            org_id='org_example',
            name='AML Policy',
            type='uploaded',
            version=1,
            status='ACTIVE',
        ),
    )

    written = history_service.backfill_missing_compliance_history(
        db,  # type: ignore[arg-type]
        org_id='org_example',
    )

    assert written == 1
    assert len(db.added) == 1
    row = db.added[0]
    assert isinstance(row, ComplianceHistory)
    assert row.gap_analysis_id == 'gap-missing-history'
    assert row.trigger_type == 'business_change'
    assert row.policy_name == 'AML Policy'
    assert row.regulatory_rules == ['FINRA Rule 3110']


def test_sanitize_cco_judgment_notes_redacts_contact_pii() -> None:
    cleaned = history_service.sanitize_cco_judgment_notes(
        'Call Jane at 555-123-4567 or jane@example.com about CIP cadence.'
    )
    assert 'jane@example.com' not in cleaned
    assert '555-123-4567' not in cleaned
    assert '[redacted-email]' in cleaned
    assert '[redacted-phone]' in cleaned


def test_retrieve_prior_cco_judgment_excerpts_same_policy_only() -> None:
    from app.models.core import AuditLog

    now = datetime.utcnow()
    matching = AuditLog(
        id='audit-1',
        org_id='org_example',
        event_type='REVISION_DECISION',
        actor_id='user-1',
        timestamp=now,
        revision_id='rev-1',
        policy_id='policy-aml',
        gap_analysis_id='gap-1',
        metadata_json={'decision': 'REJECTED'},
        notes='Needs named AMLCO designation.',
    )
    db = _Session()
    # Mock sessions ignore SQL filters; only supply same-policy rows here.
    db.set_list(AuditLog, [matching])

    excerpts = history_service.retrieve_prior_cco_judgment_excerpts(
        db,  # type: ignore[arg-type]
        'org_example',
        'policy-aml',
    )

    assert len(excerpts) == 1
    assert excerpts[0]['document_type'] == 'prior_cco_judgment'
    assert 'AMLCO' in excerpts[0]['text']
    assert 'user-1' not in excerpts[0]['text']


def test_list_includes_sanitized_cco_judgment_notes() -> None:
    from app.models.core import AuditLog

    now = datetime.utcnow()
    history = ComplianceHistory(
        id='history-1',
        org_id='org_example',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        policy_name='VRM Test Policy',
        regulatory_rules=['FINRA Rule 3310'],
        trigger_type='policy_update',
        incident_at=now,
        created_at=now,
    )
    gap = GapAnalysis(
        id='gap-1',
        org_id='org_example',
        policy_id='policy-1',
        trigger_type='policy_update',
        trigger_id='policy-1',
        impact_score='HIGH',
        gap_summary='Missing AMLCO.',
        rule='FINRA Rule 3310',
        regulatory_citations=[],
        retrieval_metadata={
            'sentence_findings': [
                {'rule': 'FINRA Rule 3310', 'gap': 'Missing AMLCO.', 'fix': 'Name AMLCO.'}
            ]
        },
        analysis_mode='LLM',
        created_at=now,
    )
    audit = AuditLog(
        id='audit-1',
        org_id='org_example',
        event_type='REVISION_DECISION',
        actor_id='cco-1',
        timestamp=now,
        revision_id='rev-1',
        policy_id='policy-1',
        gap_analysis_id='gap-1',
        metadata_json={'decision': 'REJECTED'},
        notes='Reject until AMLCO is named. Contact cco@firm.com.',
    )
    db = _Session()
    db.set_list(ComplianceHistory, [history])
    db.set_list(GapAnalysis, [gap])
    db.set_list(AuditLog, [audit])

    response = history_service.list_compliance_history_for_org(
        db,  # type: ignore[arg-type]
        'org_example',
        days=30,
        limit=50,
    )

    assert response.total >= 1
    assert response.items[0].cco_judgment_notes is not None
    assert 'AMLCO' in response.items[0].cco_judgment_notes
    assert 'cco@firm.com' not in response.items[0].cco_judgment_notes
