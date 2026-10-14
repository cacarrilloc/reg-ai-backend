from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_phase_7_migration_adds_review_workflow_columns() -> None:
    migration = (ROOT / 'supabase/migrations/phase_7_cco_review_workflow.sql').read_text()

    assert 'alter table public.policy_revisions add column if not exists org_id text' in migration
    assert 'alter table public.policy_revisions add column if not exists policy_id text' in migration
    assert 'alter table public.policy_revisions add column if not exists sla_reminded_at timestamptz' in migration
    assert 'alter table public.policy_revisions add column if not exists escalated_at timestamptz' in migration
    assert 'alter table public.audit_logs add column if not exists gap_analysis_id text' in migration
    assert 'alter table public.audit_logs add column if not exists notes text' in migration


def test_phase_7_readiness_doc_marks_workflow_implemented() -> None:
    readiness = (ROOT / 'docs/v1-saas-plan-readiness.md').read_text()
    runbook = (ROOT / 'docs/phase-7-cco-review-workflow.md').read_text()

    assert '7 CCO review workflow' in readiness
    assert 'Implemented: review queue dashboard' in readiness
    assert 'approve/reject decision endpoint' in readiness
    assert 'review-sla-reminder' in runbook
    assert 'review-escalation' in runbook
