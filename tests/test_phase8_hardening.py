import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(r'eyJ[A-Za-z0-9_-]{20,}'),
    re.compile(r'sb_secret_[A-Za-z0-9_]+'),
    re.compile(r'sk_(?:test|live)_[A-Za-z0-9]+'),
    re.compile(r'postgresql\\+psycopg://[^\\s]+:[^\\s]+@'),
)


def test_tracked_env_examples_do_not_contain_live_secret_material() -> None:
    """If placeholder env templates are tracked, they must stay secret-free.

    Local dumps live in gitignored ``deploy/.railway.env*`` / ``.env`` and are
    not required to exist in the repo.
    """
    candidates = [
        ROOT / 'deploy' / 'railway.env.example',
        ROOT / 'deploy' / 'railway.env.staging.example',
        ROOT / 'deploy' / 'railway.env.production.example',
        ROOT / '.env.example',
    ]
    for path in candidates:
        if not path.exists():
            continue
        contents = path.read_text()
        for pattern in SECRET_PATTERNS:
            assert pattern.search(contents) is None, f'{path.name} contains secret-like material'
        assert 'VERCEL_OIDC_TOKEN=' not in contents
        assert 'NEXT_PUBLIC_SUPABASE' not in contents


def test_phase_8_rls_migration_forces_tenant_policies() -> None:
    migration = (ROOT / 'supabase/migrations/phase_8_audit_hardening.sql').read_text()
    expected_tables = [
        'organizations',
        'policies',
        'regulatory_publications',
        'context_documents',
        'document_chunks',
        'business_changes',
        'business_change_event_deliveries',
        'publication_event_deliveries',
        'gap_analyses',
        'policy_revisions',
        'audit_logs',
    ]
    for table in expected_tables:
        assert f'alter table public.{table} enable row level security' in migration
        assert f'alter table public.{table} force row level security' in migration

    # phase_8 predates the phase_25 auth rename; current_clerk_org_id() remains
    # valid there because phase_25 keeps it as a compatibility wrapper.
    assert 'public.current_clerk_org_id()' in migration
    assert 'policy_revisions_org_access' in migration
    assert 'regulatory_publications_org_select' in migration
    assert 'publication_deliveries_org_access' in migration
    assert 'business_change_deliveries_org_access' in migration


def test_audit_logs_are_append_only_in_phase_8_migration() -> None:
    migration = (ROOT / 'supabase/migrations/phase_8_audit_hardening.sql').read_text()

    assert 'prevent_audit_log_mutation' in migration
    assert 'audit_logs_no_update' in migration
    assert 'audit_logs_no_delete' in migration
    assert 'audit_logs are append-only' in migration


def test_phase_10_drops_gap_analysis_fk_from_append_only_audit_logs() -> None:
    migration = (
        ROOT / 'supabase/migrations/phase_10_audit_logs_gap_analysis_fk.sql'
    ).read_text()

    assert 'drop constraint if exists audit_logs_gap_analysis_id_fkey' in migration
    assert 'add constraint audit_logs_gap_analysis_id_fkey' not in migration


def test_phase_8_runbook_documents_external_evidence_gaps() -> None:
    runbook = (ROOT / 'docs/phase_8_audit_hardening.md').read_text()

    assert 'Vercel frontend expected variables' in runbook
    assert 'Railway backend expected variables' in runbook
    assert 'SOC 2 Readiness Notes' in runbook
    assert 'Live regulatory trigger' in runbook
    assert 'Business-change trigger' in runbook
    assert 'Hudson River Securities LLC' in runbook
