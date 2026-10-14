"""Ordered SQL migrations applied to newly provisioned tenant Supabase projects.

Platform-only control-plane migrations (phase_23, phase_24) are excluded.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / 'supabase' / 'migrations'

# Lexicographic phase order with alembic-only gaps filled by tenant_*.sql mirrors.
TENANT_MIGRATION_FILES: tuple[str, ...] = (
    'phase_1_foundation.sql',
    'tenant_phase_2_ingestion.sql',
    'phase_3_regulatory_monitoring.sql',
    'phase_4_business_change_trigger.sql',
    'phase_5_gap_analysis_agent.sql',
    'policy_chunk_integrity.sql',
    'phase_7_cco_review_workflow.sql',
    'phase_8_audit_hardening.sql',
    'phase_9_compliance_automation.sql',
    'phase_10_audit_logs_gap_analysis_fk.sql',
    'phase_11_business_change_analysis.sql',
    'phase_12_document_chunk_type.sql',
    'phase_13_document_chunk_policy_name.sql',
    'tenant_phase_14_review_requested_at.sql',
    'tenant_phase_15_gap_rule_fix.sql',
    'tenant_phase_16_policy_type_uploaded.sql',
    'tenant_phase_17_approved_reupload_reminders.sql',
    'tenant_phase_18_compliance_history.sql',
    'tenant_phase_19_compliance_history_trigger_type.sql',
    'tenant_phase_20_compliance_history_gap_backfill.sql',
    'phase_21_drop_publication_event_deliveries.sql',
    'phase_22_organization_members.sql',
    'tenant_phase_40_marketing_review_agent.sql',
    'tenant_phase_41_marketing_status_checks.sql',
    'phase_42_regulations_table.sql',
    'tenant_phase_44_newsletter_subscribers.sql',
    'tenant_phase_45_ai_provider_credentials.sql',
    'tenant_phase_46_ai_reembed_vectors.sql',
)

_PLATFORM_ONLY_PREFIXES = (
    'phase_23_',
    'phase_24_',
)


def tenant_migration_paths() -> list[Path]:
    """Return absolute paths for tenant schema SQL in apply order."""
    paths: list[Path] = []
    for name in TENANT_MIGRATION_FILES:
        if name.startswith(_PLATFORM_ONLY_PREFIXES):
            continue
        path = _MIGRATIONS_DIR / name
        if not path.is_file():
            raise FileNotFoundError(f'Tenant migration missing: {path}')
        paths.append(path)
    return paths


def is_platform_only_migration(filename: str) -> bool:
    return filename.startswith(_PLATFORM_ONLY_PREFIXES)
