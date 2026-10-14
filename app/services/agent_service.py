"""Compatibility entry point for the shared Inngest policy-change workflow."""

from app.services.gap_analysis_service import run_gap_analysis

# Phase 3 and Phase 4 events already invoke this name. Keep that contract while
# the Phase 5 implementation performs retrieval, scoring, and persistence.
run_policy_change_agent = run_gap_analysis
