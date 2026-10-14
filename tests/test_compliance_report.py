from io import BytesIO

from docx import Document

from app.models.core import GapAnalysis, Policy
from app.services.compliance_report_service import (
    build_compliance_report_docx,
    calculate_compliance_snapshot,
    fallback_compliance_plan,
    parse_compliance_fields_loosely,
)
from app.services.llm_json import parse_llm_json_dict


def test_parse_compliance_json_recovers_truncated_executive_summary() -> None:
    text = (
        """{
  "executive_summary": "The firm must update AML, FINRA, and vendor policies before launching the new """
        """savings account product.",
  "required_changes": [
    {
      "policy_name": "AML Policy",
      "priority": "HIGH",
      "change_description": "Expand CIP scope to include savings accounts."""
    )
    payload = parse_llm_json_dict(text, loose_parser=parse_compliance_fields_loosely)
    assert 'savings account product' in payload['executive_summary']
    assert payload['required_changes'] == []


def test_fallback_compliance_plan_uses_gap_summaries() -> None:
    policy = Policy(
        id='policy-id',
        org_id='org_example',
        name='AML Policy',
        version=1,
        status='ACTIVE',
    )
    gap = GapAnalysis(
        id='gap-id',
        org_id='org_example',
        policy_id='policy-id',
        trigger_type='business_change',
        trigger_id='change-id',
        impact_score='HIGH',
        gap_summary='Scope must include savings accounts.',
        regulatory_citations=[],
    )
    plan = fallback_compliance_plan([policy], [(gap, policy)])
    assert plan['required_changes'][0]['change_description'] == 'Scope must include savings accounts.'


def test_compliance_snapshot_counts_all_open_material_gaps() -> None:
    policies = [
        Policy(id='policy-a', org_id='org_example', name='AML', version=2, status='ACTIVE'),
        Policy(id='policy-b', org_id='org_example', name='Privacy', version=1, status='ACTIVE'),
        Policy(id='policy-c', org_id='org_example', name='Vendor', version=3, status='ACTIVE'),
    ]
    gaps = [
        (
            GapAnalysis(
                id='gap-high',
                org_id='org_example',
                policy_id='policy-a',
                trigger_type='policy_update',
                trigger_id='policy-a',
                impact_score='HIGH',
                gap_summary='High gap',
            ),
            policies[0],
        ),
        (
            GapAnalysis(
                id='gap-medium',
                org_id='org_example',
                policy_id='policy-a',
                trigger_type='publication',
                trigger_id='publication-1',
                impact_score='MEDIUM',
                gap_summary='Medium gap',
            ),
            policies[0],
        ),
    ]

    snapshot = calculate_compliance_snapshot(policies, gaps)

    assert snapshot.status == 'ACTION REQUIRED'
    assert snapshot.score == 66.7
    assert snapshot.open_gap_count == 2
    assert snapshot.high_gap_count == 1
    assert snapshot.medium_gap_count == 1
    assert snapshot.policies_with_open_gaps == 1


def test_docx_contains_status_score_and_complete_raw_gap_register() -> None:
    policy = Policy(
        id='policy-id',
        org_id='org_example',
        name='AML Policy',
        type='uploaded',
        version=4,
        status='ACTIVE',
    )
    gaps = [
        (
            GapAnalysis(
                id=f'gap-{index}',
                org_id='org_example',
                policy_id='policy-id',
                trigger_type='policy_update',
                trigger_id='policy-id',
                impact_score='HIGH' if index == 1 else 'MEDIUM',
                gap_summary=f'Unique gap summary {index}',
                rule=f'Rule {index}',
                fix=f'Fix {index}',
                regulatory_citations=[
                    {'source': 'FINRA', 'reference': f'Notice {index}'}
                ],
            ),
            policy,
        )
        for index in range(1, 43)
    ]
    snapshot = calculate_compliance_snapshot([policy], gaps)
    report_data = {
        'executive_summary': 'Summary',
        'required_changes': [],
        'cross_cutting_themes': [],
    }

    payload = build_compliance_report_docx(
        'org_example',
        [policy],
        gaps,
        report_data,
        snapshot=snapshot,
    )
    document = Document(BytesIO(payload))
    text = '\n'.join(paragraph.text for paragraph in document.paragraphs)

    assert 'Current Compliance Status' in text
    assert '0.0% Compliance' in text
    assert 'Open material gaps: 42 (1 HIGH, 41 MEDIUM)' in text
    assert 'Complete Open Gap Register' in text
    assert 'Unique gap summary 1' in text
    assert 'Unique gap summary 42' in text
    assert 'Fix 42' in text
    assert 'FINRA — Notice 42' in text
