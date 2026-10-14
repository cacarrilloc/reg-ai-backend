"""Phase 5 policy-gap retrieval, assessment, and persistence workflow.

The vector-store adapter deliberately exposes the existing tenant-scoped
``document_chunks`` table to LlamaIndex instead of creating a second, opaque
LlamaIndex table. Every vector query is constrained by ``org_id`` and active
policy status before pgvector ranks the relevant chunks.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import case, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import (
    ChatMessage,
    LLMProvider,
    embedding_model_label,
    is_billing_error,
    is_non_retryable_error,
    is_rate_limit_error,
    optional_embedding_provider,
    optional_llm_provider,
    provider_label,
    retry_delay,
    user_error_message,
)
from app.core.config import Settings, get_settings
from app.core.impact_score import normalize_impact_score
from app.core.observability import (
    langfuse_workflow_trace,
    normalize_trace_tags,
    trace_event,
)
from app.db.pool_guards import background_db_slot
from app.db.session import (
    SessionLocal,
    bind_data_plane_session,
    eager_load_columns,
    reset_tenant_org_id,
    set_tenant_org_id,
)
from app.models.core import (
    BusinessChange,
    ContextDocument,
    DocumentChunk,
    GapAnalysis,
    Policy,
    PolicyRevision,
    RegulatoryPublication,
)
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.schemas.gap_analyses import (
    KNOWN_GAP_TRIGGER_TYPES,
    GapAnalysisResponse,
    GapCitationResponse,
    GapFindingSummary,
)
from app.services.document_chunk_service import (
    compare_internal_policy_to_regulator_documents_with_fallback,
)
from app.services.llm_json import extract_json_string_field
from app.services.llm_json import response_json as parse_response_json
from app.services.llm_persona import (
    internal_policy_analysis_workflow,
    with_internal_policy_analysis_persona,
)
from app.services.markdown_text import strip_markdown_emphasis
from app.services.marketing_gap_bridge import (
    display_marketing_policy_name,
    marketing_gap_api_fields,
)
from app.services.policy_comparison_service import (
    MAX_BUSINESS_CHANGE_GOVERNMENT_EXCERPTS,
    REGULATOR_SOURCES,
    GovernmentExcerpt,
    merge_government_excerpts,
    retrieve_government_excerpts_for_business_change,
    retrieve_government_excerpts_for_internal_policy,
)
from app.services.policy_types import (
    INTERNAL_POLICY_TYPE,
    INTERNAL_REGULATOR_POLICY_NAMES,
    LEGACY_INTERNAL_POLICY_TYPES,
    REGULATORY_REFERENCE_TYPE,
    is_internal_policy_type,
)
from app.services.review_service import trigger_label_for_gap

logger = structlog.get_logger(__name__)


@contextmanager
def _analysis_session_scope(
    *,
    commit: bool = True,
    org_id: str | None = None,
) -> Generator[Session, None, None]:
    """Open a tenant-routed session; keep module SessionLocal patchable in tests."""
    if SessionLocal is None:
        raise RuntimeError('Database session requested but DATABASE_URL is not configured')

    from app.db import session as session_mod

    # Tests replace this module's SessionLocal with a fake factory.
    if SessionLocal is not session_mod.SessionLocal:
        db = SessionLocal()
        bind_data_plane_session(db, org_id=org_id)
        try:
            yield db
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        return

    with session_mod.session_scope(commit=commit, org_id=org_id) as db:
        yield db


def _detach_trigger_context(trigger: TriggerContext) -> TriggerContext:
    """Copy trigger ORM fields into detached snapshots safe to use after session close."""
    publication = None
    if trigger.publication is not None:
        publication = RegulatoryPublication(
            id=trigger.publication.id,
            source=trigger.publication.source,
            title=trigger.publication.title,
            url=trigger.publication.url,
            published_at=trigger.publication.published_at,
            raw_text=trigger.publication.raw_text,
            processed=bool(trigger.publication.processed),
            embedding_chunk_ids=list(trigger.publication.embedding_chunk_ids or []),
        )
    business_change = None
    if trigger.business_change is not None:
        business_change = BusinessChange(
            id=trigger.business_change.id,
            org_id=trigger.business_change.org_id,
            change_type=trigger.business_change.change_type,
            description=trigger.business_change.description,
            flagged_by=trigger.business_change.flagged_by or 'system',
            change_date=trigger.business_change.change_date,
            triggers_review=bool(trigger.business_change.triggers_review),
            assessment_rationale=trigger.business_change.assessment_rationale or '',
            assessment_model=trigger.business_change.assessment_model,
            assessment_mode=trigger.business_change.assessment_mode or 'RULE_FALLBACK',
            event_summary=trigger.business_change.event_summary or '',
        )
    return TriggerContext(
        org_id=trigger.org_id,
        trigger_type=trigger.trigger_type,
        trigger_id=trigger.trigger_id,
        publication=publication,
        business_change=business_change,
        text=trigger.text,
        citation_source=trigger.citation_source,
        citation_reference=trigger.citation_reference,
        label=trigger.label,
    )


def _apply_gap_analysis_fields(
    gap: GapAnalysis,
    *,
    publication_id: str | None,
    business_change_id: str | None,
    finding: GapFinding,
    retrieval_metadata: dict[str, Any],
    policy_id: str | None = None,
    trigger_type: str | None = None,
    trigger_id: str | None = None,
) -> None:
    if policy_id is not None:
        gap.policy_id = policy_id
    if trigger_type is not None:
        gap.trigger_type = trigger_type
    if trigger_id is not None:
        gap.trigger_id = trigger_id
    gap.publication_id = publication_id
    gap.business_change_id = business_change_id
    gap.impact_score = finding.impact_score
    gap.gap_summary = finding.summary
    gap.rule = finding.rule
    gap.fix = finding.fix
    gap.regulatory_citations = finding.citations
    gap.retrieval_metadata = retrieval_metadata
    gap.analysis_model = finding.analysis_model
    gap.analysis_mode = finding.analysis_mode
    # Bump created_at so list sorting treats this as the latest analysis.
    gap.created_at = datetime.now(UTC)
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(gap, 'retrieval_metadata')
    except Exception:
        pass


NEEDS_REVIEW_DECISION_KEY = 'needs_review_decision'
NEEDS_REVIEW_DECISION_AT_KEY = 'needs_review_decision_at'
NEEDS_REVIEW_DECISION_MESSAGE = (
    'A new gap analysis has been generated. Request Review again to send '
    'this gap to the CCO Review Queue with a new Redline doc.'
)


def _stamp_needs_review_decision(metadata: dict[str, Any]) -> dict[str, Any]:
    stamped = dict(metadata)
    stamped[NEEDS_REVIEW_DECISION_KEY] = True
    stamped[NEEDS_REVIEW_DECISION_AT_KEY] = datetime.now(UTC).isoformat()
    return stamped


def _reset_pending_cco_review_after_refresh(
    db: Session,
    *,
    org_id: str,
    gap_analysis_id: str,
) -> int:
    """Drop a pending CCO queue item so the user must Request Review again.

    Leaves the PolicyRevision row so Request Review can reuse it, but clears
    ``review_requested_at`` (hides it from the queue) and the old redline URL
    so a later request generates a new redline from the refreshed findings.
    """
    query = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'PENDING',
        )
    )
    all_fn = getattr(query, 'all', None)
    if not callable(all_fn):
        return 0
    try:
        revisions = list(all_fn())
    except Exception:
        logger.exception(
            'gap_analysis_refresh_unqueue_failed',
            org_id=org_id,
            gap_analysis_id=gap_analysis_id,
        )
        return 0
    reset_count = 0
    for revision in revisions:
        changed = False
        if getattr(revision, 'review_requested_at', None) is not None:
            revision.review_requested_at = None
            changed = True
        if getattr(revision, 'redline_file_url', None):
            revision.redline_file_url = None
            changed = True
        if changed:
            reset_count += 1
    if reset_count:
        logger.info(
            'gap_analysis_refresh_unqueued_pending_review',
            org_id=org_id,
            gap_analysis_id=gap_analysis_id,
            reset_count=reset_count,
        )
    return reset_count


def gap_needs_fresh_redline(gap: GapAnalysis | None) -> bool:
    """True when overwrite refreshed findings and the user has not Request Reviewed yet."""
    if gap is None or not isinstance(gap.retrieval_metadata, dict):
        return False
    return bool(gap.retrieval_metadata.get(NEEDS_REVIEW_DECISION_KEY))


def clear_pending_redline_url_for_gap(
    db: Session,
    *,
    org_id: str,
    gap_analysis_id: str,
) -> int:
    """Drop a stale pending redline so Request Review generates a new DOCX."""
    query = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == 'PENDING',
        )
    )
    all_fn = getattr(query, 'all', None)
    if not callable(all_fn):
        return 0
    try:
        revisions = list(all_fn())
    except Exception:
        return 0
    cleared = 0
    for revision in revisions:
        if not getattr(revision, 'redline_file_url', None):
            continue
        revision.redline_file_url = None
        cleared += 1
    return cleared


def persist_cleared_needs_review_decision(db: Session, gap: GapAnalysis | None) -> None:
    """Clear the re-review flag after Request Review and commit (get_db does not)."""
    clear_gap_needs_review_decision(gap)
    commit = getattr(db, 'commit', None)
    if not callable(commit):
        return
    try:
        commit()
    except Exception:
        rollback = getattr(db, 'rollback', None)
        if callable(rollback):
            rollback()
        logger.exception('gap_needs_review_decision_clear_commit_failed')


def clear_gap_needs_review_decision(gap: GapAnalysis | None) -> None:
    """Clear the post-refresh re-decision flag after Request Review."""
    if gap is None or not isinstance(gap.retrieval_metadata, dict):
        return
    if not gap.retrieval_metadata.get(NEEDS_REVIEW_DECISION_KEY):
        return
    metadata = dict(gap.retrieval_metadata)
    metadata[NEEDS_REVIEW_DECISION_KEY] = False
    gap.retrieval_metadata = metadata
    try:
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(gap, 'retrieval_metadata')
    except Exception:
        pass


def needs_review_decision_response_fields(
    metadata: dict[str, Any] | None,
    *,
    status: str,
) -> dict[str, Any]:
    del status  # Prompt follows the overwrite flag, not queue status.
    pending = bool((metadata or {}).get(NEEDS_REVIEW_DECISION_KEY))
    return {
        'needs_review_decision': pending,
        'needs_review_decision_message': (
            NEEDS_REVIEW_DECISION_MESSAGE if pending else None
        ),
    }


def _normalize_policy_name_key(name: str | None) -> str:
    return ' '.join(str(name or '').split()).casefold()


def _gap_list_dedupe_key(policy_name: str | None, analysis: GapAnalysis) -> str:
    """One Policy Update row per name; one Business Change row per change."""
    name_key = _normalize_policy_name_key(policy_name)
    trigger = _normalized_gap_trigger(analysis.trigger_type)
    if trigger == 'business_change':
        change_id = (
            (analysis.trigger_id or '').strip()
            or (analysis.business_change_id or '').strip()
            or analysis.id
        )
        return f'{name_key}::business_change::{change_id}'
    if trigger == 'marketing_content':
        marketing_id = (analysis.trigger_id or '').strip() or analysis.id
        return f'{name_key}::marketing::{marketing_id}'
    return f'{name_key}::policy_update'


def _gap_analyses_for_policy_name(
    db: Session,
    *,
    org_id: str,
    policy_name: str,
) -> list[GapAnalysis]:
    """Return all gap rows for an Internal policy name (any version / trigger), newest first."""
    key = _normalize_policy_name_key(policy_name)
    if not key:
        return []
    rows = (
        db.query(GapAnalysis, Policy.name)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .filter(
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .order_by(GapAnalysis.created_at.desc())
        .all()
    )
    return [
        analysis
        for analysis, name in rows
        if _normalize_policy_name_key(name) == key
    ]


def _delete_gap_analysis_rows(
    db: Session,
    *,
    org_id: str,
    gaps: list[GapAnalysis],
) -> None:
    for gap in gaps:
        db.delete(gap)
    if gaps:
        logger.info(
            'gap_analysis_duplicates_removed',
            org_id=org_id,
            removed_count=len(gaps),
            gap_ids=[gap.id for gap in gaps],
        )


def _rebind_gap_policy_revisions(
    db: Session,
    *,
    org_id: str,
    gap_analysis_id: str,
    previous_policy_id: str,
    policy_id: str,
) -> None:
    """Move policy_revisions before mutating gap_analyses.policy_id.

    ``policy_revisions`` has composite FK
    ``(gap_analysis_id, org_id, policy_id) → gap_analyses(id, org_id, policy_id)``.
    Updating ``gap.policy_id`` first leaves that FK dangling and raises IntegrityError.
    """
    if previous_policy_id == policy_id:
        return
    (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
        )
        .update({PolicyRevision.policy_id: policy_id}, synchronize_session=False)
    )
    flush = getattr(db, 'flush', None)
    if callable(flush):
        flush()


def _overwrite_existing_gap_analysis(
    db: Session,
    *,
    org_id: str,
    existing: GapAnalysis,
    siblings: list[GapAnalysis],
    publication_id: str | None,
    business_change_id: str | None,
    trigger_type: str,
    trigger_id: str,
    policy_id: str,
    finding: GapFinding,
    retrieval_metadata: dict[str, Any],
) -> str:
    """Overwrite one gap row (and drop same-name siblings) without FK/unique races."""
    from app.services.compliance_history_service import safe_upsert_compliance_history_for_gap

    # Free the unique (org, trigger, policy) slot and avoid dual same-name rows first.
    _delete_gap_analysis_rows(db, org_id=org_id, gaps=siblings)

    previous_policy_id = existing.policy_id
    _rebind_gap_policy_revisions(
        db,
        org_id=org_id,
        gap_analysis_id=existing.id,
        previous_policy_id=previous_policy_id,
        policy_id=policy_id,
    )
    metadata = dict(retrieval_metadata)
    if _is_policy_update_trigger(trigger_type) or _is_policy_update_trigger(
        existing.trigger_type
    ):
        metadata = _stamp_needs_review_decision(metadata)
    _apply_gap_analysis_fields(
        existing,
        publication_id=publication_id,
        business_change_id=business_change_id,
        finding=finding,
        retrieval_metadata=metadata,
        policy_id=policy_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
    )
    if _is_policy_update_trigger(existing.trigger_type or trigger_type):
        _reset_pending_cco_review_after_refresh(
            db,
            org_id=org_id,
            gap_analysis_id=existing.id,
        )
    safe_upsert_compliance_history_for_gap(
        db,
        org_id=org_id,
        gap_analysis_id=existing.id,
        policy_id=policy_id,
        rule=finding.rule,
        citations=finding.citations,
        retrieval_metadata=retrieval_metadata,
        incident_at=existing.created_at,
        trigger_type=existing.trigger_type or trigger_type,
    )
    return existing.id


POLICY_UPDATE_TRIGGER_LABELS = frozenset({'policy_update', 'publication'})


def _normalized_gap_trigger(value: str | None) -> str:
    return (value or '').strip().lower()


def _is_policy_update_trigger(value: str | None) -> bool:
    return _normalized_gap_trigger(value) in POLICY_UPDATE_TRIGGER_LABELS


def _is_business_change_trigger(value: str | None) -> bool:
    return _normalized_gap_trigger(value) == 'business_change'


def _business_change_ids(*values: str | None) -> set[str]:
    return {item.strip() for item in values if item and item.strip()}


def _gap_matches_business_change(gap: GapAnalysis, change_ids: set[str]) -> bool:
    if not change_ids or not _is_business_change_trigger(gap.trigger_type):
        return False
    return bool(_business_change_ids(gap.trigger_id, gap.business_change_id) & change_ids)


def _existing_gap_to_overwrite(
    *,
    exact: GapAnalysis | None,
    existing_for_name: Sequence[GapAnalysis],
    trigger_type: str,
    trigger_id: str,
    business_change_id: str | None = None,
) -> GapAnalysis | None:
    """Overwrite same-label gaps; never retag Policy Update ↔ Business Change.

    The same Business Change (same change id) on the same Internal policy always
    overwrites that Business Change row, including across policy versions.
    """
    if isinstance(exact, GapAnalysis):
        return exact
    if _is_business_change_trigger(trigger_type):
        change_ids = _business_change_ids(trigger_id, business_change_id)
        for gap in existing_for_name:
            if _gap_matches_business_change(gap, change_ids):
                return gap
        return None
    if _is_policy_update_trigger(trigger_type):
        for gap in existing_for_name:
            if _is_policy_update_trigger(gap.trigger_type):
                return gap
        return None
    return None


def _sibling_gaps_to_replace(
    *,
    existing_for_name: Sequence[GapAnalysis],
    keep: GapAnalysis | None,
    trigger_type: str,
    trigger_id: str,
    business_change_id: str | None = None,
) -> list[GapAnalysis]:
    """Drop older same-label rows only. Keep the other trigger family."""
    keep_id = keep.id if isinstance(keep, GapAnalysis) else None
    change_ids = _business_change_ids(trigger_id, business_change_id)
    siblings: list[GapAnalysis] = []
    for gap in existing_for_name:
        if keep_id and gap.id == keep_id:
            continue
        if _is_business_change_trigger(trigger_type):
            if _gap_matches_business_change(gap, change_ids):
                siblings.append(gap)
            continue
        if _is_policy_update_trigger(trigger_type) and _is_policy_update_trigger(gap.trigger_type):
            siblings.append(gap)
    return siblings


def persist_gap_analysis_result(
    db: Session,
    *,
    org_id: str,
    publication_id: str | None,
    business_change_id: str | None,
    trigger_type: str,
    trigger_id: str,
    policy_id: str,
    finding: GapFinding,
    retrieval_metadata: dict[str, Any],
    force_update: bool,
) -> str:
    """Insert or overwrite Internal Policy Gaps rows without mixing trigger labels.

    Used by Internal Policy upload and the compliance cron. Policy Update overwrites
    the same-name Policy Update row and flags it so the user must Request Review
    again. If that row does not exist, a new Policy Update gap is created. A
    Business Change insert never overwrites the Policy Update row. Re-running the
    same Business Change on the same Internal policy overwrites that Business
    Change row. ``force_update`` is accepted for call-site compatibility.
    """
    del force_update
    from app.services.compliance_history_service import safe_upsert_compliance_history_for_gap

    policy = (
        db.query(Policy)
        .filter(Policy.id == policy_id, Policy.org_id == org_id)
        .one_or_none()
    )
    policy_name = policy.name if isinstance(policy, Policy) else ''
    existing_for_name = (
        _gap_analyses_for_policy_name(db, org_id=org_id, policy_name=policy_name)
        if policy_name
        else []
    )

    exact = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.org_id == org_id,
            GapAnalysis.trigger_type == trigger_type,
            GapAnalysis.trigger_id == trigger_id,
            GapAnalysis.policy_id == policy_id,
        )
        .one_or_none()
    )
    existing = _existing_gap_to_overwrite(
        exact=exact if isinstance(exact, GapAnalysis) else None,
        existing_for_name=existing_for_name,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        business_change_id=business_change_id,
    )
    siblings = _sibling_gaps_to_replace(
        existing_for_name=existing_for_name,
        keep=existing,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        business_change_id=business_change_id,
    )

    if isinstance(existing, GapAnalysis):
        return _overwrite_existing_gap_analysis(
            db,
            org_id=org_id,
            existing=existing,
            siblings=siblings,
            publication_id=publication_id,
            business_change_id=business_change_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            policy_id=policy_id,
            finding=finding,
            retrieval_metadata=retrieval_metadata,
        )

    gap_id = str(uuid4())
    gap = GapAnalysis(
        id=gap_id,
        org_id=org_id,
        publication_id=publication_id,
        business_change_id=business_change_id,
        trigger_type=trigger_type,
        trigger_id=trigger_id,
        policy_id=policy_id,
        impact_score=finding.impact_score,
        gap_summary=finding.summary,
        rule=finding.rule,
        fix=finding.fix,
        regulatory_citations=finding.citations,
        retrieval_metadata=retrieval_metadata,
        analysis_model=finding.analysis_model,
        analysis_mode=finding.analysis_mode,
    )
    begin_nested = getattr(db, 'begin_nested', None)
    try:
        if begin_nested is None:
            db.add(gap)
            flush = getattr(db, 'flush', None)
            if callable(flush):
                flush()
            _delete_gap_analysis_rows(db, org_id=org_id, gaps=siblings)
            safe_upsert_compliance_history_for_gap(
                db,
                org_id=org_id,
                gap_analysis_id=gap_id,
                policy_id=policy_id,
                rule=finding.rule,
                citations=finding.citations,
                retrieval_metadata=retrieval_metadata,
                incident_at=gap.created_at,
                trigger_type=trigger_type,
            )
            return gap_id
        with db.begin_nested():
            db.add(gap)
            db.flush()
        _delete_gap_analysis_rows(db, org_id=org_id, gaps=siblings)
        safe_upsert_compliance_history_for_gap(
            db,
            org_id=org_id,
            gap_analysis_id=gap_id,
            policy_id=policy_id,
            rule=finding.rule,
            citations=finding.citations,
            retrieval_metadata=retrieval_metadata,
            incident_at=gap.created_at,
            trigger_type=trigger_type,
        )
        return gap_id
    except IntegrityError:
        logger.warning(
            'gap_analysis_unique_conflict_updating_existing',
            org_id=org_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            policy_id=policy_id,
        )
        raced = (
            db.query(GapAnalysis)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.trigger_type == trigger_type,
                GapAnalysis.trigger_id == trigger_id,
                GapAnalysis.policy_id == policy_id,
            )
            .one_or_none()
        )
        raced_for_name = _gap_analyses_for_policy_name(
            db, org_id=org_id, policy_name=policy_name
        )
        if not isinstance(raced, GapAnalysis):
            raced = _existing_gap_to_overwrite(
                exact=None,
                existing_for_name=raced_for_name,
                trigger_type=trigger_type,
                trigger_id=trigger_id,
                business_change_id=business_change_id,
            )
        if not isinstance(raced, GapAnalysis):
            raise
        raced_siblings = _sibling_gaps_to_replace(
            existing_for_name=raced_for_name,
            keep=raced,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            business_change_id=business_change_id,
        )
        return _overwrite_existing_gap_analysis(
            db,
            org_id=org_id,
            existing=raced,
            siblings=raced_siblings,
            publication_id=publication_id,
            business_change_id=business_change_id,
            trigger_type=trigger_type,
            trigger_id=trigger_id,
            policy_id=policy_id,
            finding=finding,
            retrieval_metadata=retrieval_metadata,
        )

AGENT_ACTOR_ID = 'policy-gap-analysis-agent'
MAX_VECTOR_RESULTS = 24
MAX_CHUNKS_PER_POLICY = 8
MAX_POLICIES_PER_RUN = 8
MANDATORY_GAP_ANALYSIS_TRIGGER_TYPES = frozenset({'business_change', 'policy_update'})
MAX_CHUNK_CONTEXT_CHARS = 2_000
MAX_GAP_ANALYSIS_TRIGGER_CHARS = 3_000
MAX_GAP_GOVERNMENT_EXCERPTS = 9
MAX_GAP_GOVERNMENT_EXCERPT_CHARS = 1_500
MAX_GAP_CONTEXT_EXCERPTS = 2
MAX_GAP_CONTEXT_EXCERPT_CHARS = 1_000
MAX_PRIOR_CCO_JUDGMENT_CONTEXT = 2
MAX_SUMMARY_CHARS = 5_000
# phase1_brief + ~40 RULE/GAP/FIX findings regularly exceeds 4k completion tokens.
GAP_ANALYSIS_MAX_TOKENS = 16_384
LLM_GAP_ANALYSIS_MAX_ATTEMPTS = 6
LLM_GAP_ANALYSIS_RETRY_DELAY_SECONDS = 2.0
MAX_SENTENCE_FINDINGS = 40
GAP_ANALYSIS_TRUNCATION_RETRY_HINT = (
    '\n\nIMPORTANT RETRY: Your previous response was truncated at max_tokens before all '
    'sentence findings could be written. Keep each phase1_brief field to one short sentence. '
    'Prioritize emitting EVERY deficient sentence as its own RULE/GAP/FIX finding. The same '
    'RULE citation MAY appear on many findings — do not collapse sentence issues into one.'
)
SENTENCE_FINDINGS_METADATA_KEY = 'sentence_findings'
PHASE1_BRIEF_METADATA_KEY = 'phase1_brief'

GAP_ANALYSIS_SYSTEM_PROMPT = with_internal_policy_analysis_persona("""You are performing Reg AI regulation gap analysis.

Reg AI runs this gap analysis automatically every time one of these events occurs:
- business_change: an internal business change is submitted and matched to Internal Policy docs.
- policy_update: a document is uploaded to the Policy Library (always analyze the uploaded policy
  and re-evaluate it against recent regulatory and business-change triggers).
- publication: a new regulatory publication is ingested for the firm.
- scheduled_cron: compliance cron review of every active Internal (uploaded) policy against
  FINRA, SEC, and CFTC Regulations documents.

Compare the INTERNAL enterprise policy against government regulation obligations for FINRA, SEC,
and CFTC (and BSA/AML where the policy subject matter implicates them).

Grounding rules:
- Finding sentences MUST be verbatim from the supplied Internal Policy excerpts.
- Prefer citing concrete obligations that appear in government_reference_excerpts when present.
- Scraped government excerpts are often newsroom/notice text and may omit core rule language.
  When they lack obligations clearly applicable to the Internal Policy's subject matter, you MUST
  still apply well-established regulatory obligations a senior financial regulatory attorney would
  apply to that topic (examples: FINRA Rule 3310 for AML programs; 31 CFR CIP/SAR requirements for
  customer identification and suspicious-activity reporting; SEC/CFTC supervision and books-and-
  records duties when implicated). Do not invent fictional rules or cite unrelated topics.
- Do not return empty findings merely because government_reference_excerpts are press/notice content
  rather than full rule text, when the Internal Policy incompletely addresses obligations that apply
  to its subject matter.

Phase 2 — Sentence-by-sentence gap analysis (required only after Phase 1A policy understanding and
Phase 1B companion-source understanding). Hold the holistic meaning of the full Internal Policy and
any business_change / marketing_content / regulatory companions while judging each sentence:
1. Walk EVERY sentence in the supplied Internal Policy excerpts one at a time. Judge each sentence
   against applicable regulatory obligations (supplied excerpts and established subject-matter
   standards above) using Phase 1A/1B context — never as an isolated keyword string.
2. Do NOT rewrite the policy. Do NOT change section titles, numbering, or formatting. Do NOT replace
   sentence-level findings with a single document-wide summary when multiple sentences are deficient.
3. For EACH sentence that contradicts, omits, or otherwise conflicts with an applicable obligation,
   emit one finding with RULE, GAP, and FIX for that sentence.
4. One paragraph may produce multiple findings when multiple sentences have distinct discrepancies.
5. Skip a sentence only when it already fully complies on its own wording, or is unrelated to
   applicable regulatory obligations. Do NOT skip a deficient sentence merely because another
   section mentions a related topic — partial coverage elsewhere is still a gap for THIS sentence.
6. Use Phase 1A/1B context to judge meaning and avoid keyword-only false positives, but never use
   Phase 1 to reduce finding count. Emit a finding whenever an applicable obligation is missing,
   incomplete, vague, or contradicted for that sentence.
7. Prefer actionable HIGH/MEDIUM findings. Empty findings should be uncommon for substantive
   compliance policies (AML, CIP, supervision, etc.) that omit or understate established obligations.
8. Produce one RULE/GAP/FIX finding per deficient sentence. GAP and FIX wording must be tailored to
   each sentence. The same RULE citation MAY (and often SHOULD) appear on multiple findings when
   distinct sentences implicate the same rule (e.g. several FINRA Rule 3310 deficiencies). Never
   collapse multiple sentence issues into one finding.

Impact rubric for each finding and for the overall document score:
- HIGH: the regulation explicitly requires, prohibits, or materially changes a control that the
  sentence clearly misses or conflicts with.
- MEDIUM: the regulation plausibly changes scope, ownership, evidence, supervision, or cadence for
  that sentence, but a CCO must decide the exact amendment.
- INFO: no material discrepancy after comparing the texts (overall score only when findings is empty).

Return one JSON object only, with exactly these keys (emit phase1_brief before findings):
{
  "phase1_brief": {
    "policy_understanding": "concise summary of the Internal Policy purpose, scope, and control design",
    "companion_understanding": "concise summary of the trigger/regulation/business-change/marketing sources analyzed",
    "covered_well": "what the policy already addresses adequately",
    "silent_or_thin": "where the policy is silent, thin, vague, or inconsistent vs applicable obligations"
  },
  "impact_score": "HIGH" | "MEDIUM" | "INFO",
  "findings": [
    {
      "sentence": "verbatim internal-policy sentence that has the discrepancy",
      "impact_score": "HIGH" | "MEDIUM",
      "rule": "specific regulation/rule citation implicated for this sentence",
      "gap": "what this sentence lacks or gets wrong vs the regulation",
      "fix": "concrete remediation for this sentence only"
    }
  ],
  "citations": [
    {
      "source": "trigger or government source",
      "reference": "URL, rule citation, or publication identifier",
      "quote": "short verbatim excerpt when available",
      "rationale": "one sentence on why this source supports the findings"
    }
  ]
}

Keep each rule/gap/fix under 120 words and actionable. Include up to one citation object with a
quote under 200 characters. If there are no discrepancies, return "findings": [] and
"impact_score": "INFO".

Return strict, valid JSON only. Escape double quotes and newlines inside string values as \\n.
Do not use trailing commas. Do not wrap the JSON in markdown fences or add commentary outside the JSON.

Do not invent fictional regulations or fabricate policy text. Policy sentences must come from the
supplied Internal Policy excerpts. Regulatory RULE citations may come from supplied government
excerpts or from well-established FINRA/SEC/CFTC/BSA obligations applicable to the policy subject.""")


BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT = with_internal_policy_analysis_persona(
    """You are performing Reg AI business-change gap analysis against one matched Internal Policy.

The firm submitted an internal business change that was matched to this Internal Policy document.
After Phase 1A (Internal Policy) and Phase 1B (business_change — and any government excerpts),
compare EVERY sentence in the Internal Policy excerpts to the business-change description and
identify any policy wording that the change would affect.

Phase 2 — Sentence-by-sentence gap analysis (required only after Phase 1A/1B). Hold the holistic
meaning of the full Internal Policy and the business_change (and any marketing/regulatory companions)
while judging each sentence:
1. Walk EVERY sentence in the supplied Internal Policy excerpts one at a time. Judge each sentence
   against the business-change description (and any supplied government excerpts) using Phase 1A/1B
   context — never as an isolated keyword string.
2. Do NOT rewrite the whole policy. Do NOT change section titles, numbering, or formatting. Do NOT
   replace sentence-level findings with a single theme-level note when multiple sentences must change.
3. For EACH sentence that must change because of the business change, emit one finding with
   RULE, GAP, and FIX for that sentence.
4. RULE should cite the business-change obligation / control implication (and any supplied
   government excerpts when relevant).
5. GAP explains how the current policy sentence fails to reflect the business change.
6. FIX states the concrete policy-sentence remediation.
7. Produce one RULE/GAP/FIX finding per sentence that must change. GAP and FIX wording must be
   tailored to each sentence. The same RULE citation may be reused across findings. Never collapse
   multiple sentence updates into one finding.
8. Interpret each sentence in document context from Phase 1A — skip a sentence only when that
   sentence itself remains accurate after the business change. Do not skip merely because another
   section mentions a related control.
9. Always emit one additional finding whose FIX is a short 2–4 sentence paragraph stating the
   main regulatory aspects of this business change (supervision, books and records, communications,
   customer protection, or other FINRA/SEC/CFTC implications). That paragraph will be inserted
   into the matched Internal Policy via a later redline. Use the closest existing policy sentence
   as "sentence".

Impact rubric:
- HIGH: the business change clearly requires a material policy control update for that sentence.
- MEDIUM: the business change plausibly affects ownership, evidence, supervision, or cadence.
- INFO: no material sentence updates are needed (only when findings is empty).

Return one JSON object only, with exactly these keys (emit phase1_brief before findings):
{
  "phase1_brief": {
    "policy_understanding": "concise summary of the Internal Policy purpose, scope, and control design",
    "companion_understanding": "concise summary of the business change and any regulatory sources",
    "covered_well": "what the policy already addresses adequately relative to the change",
    "silent_or_thin": "where the policy is silent, thin, or outdated relative to the change"
  },
  "impact_score": "HIGH" | "MEDIUM" | "INFO",
  "findings": [
    {
      "sentence": "verbatim internal-policy sentence that must be updated",
      "impact_score": "HIGH" | "MEDIUM",
      "rule": "why the business change requires this sentence to change",
      "gap": "what this sentence fails to cover after the business change",
      "fix": "concrete remediation for this sentence only"
    }
  ],
  "citations": [
    {
      "source": "Business Change",
      "reference": "business change identifier or change type",
      "quote": "short verbatim excerpt from the business change when available",
      "rationale": "one sentence on why this supports the findings"
    }
  ]
}

Keep each rule/gap/fix under 120 words and actionable. If no sentences need updates, return
"findings": [] and "impact_score": "INFO".

Return strict, valid JSON only. Escape double quotes and newlines inside string values as \\n.
Do not use trailing commas. Do not wrap the JSON in markdown fences."""
)


@dataclass(frozen=True)
class TriggerContext:
    org_id: str
    trigger_type: str
    trigger_id: str
    publication: RegulatoryPublication | None
    business_change: BusinessChange | None
    text: str
    citation_source: str
    citation_reference: str
    label: str


@dataclass(frozen=True)
class RetrievedChunk:
    id: str
    index: int
    content: str
    similarity: float


@dataclass(frozen=True)
class PolicyCandidate:
    policy_id: str
    policy_name: str
    policy_version: int
    chunks: list[RetrievedChunk]
    retrieval_mode: str


@dataclass(frozen=True)
class SentenceGapFinding:
    sentence: str
    rule: str
    gap: str
    fix: str
    impact_score: str


@dataclass(frozen=True)
class GapFinding:
    impact_score: str
    summary: str
    citations: list[dict[str, str]]
    analysis_mode: str
    analysis_model: str | None
    rule: str | None = None
    fix: str | None = None
    sentence_findings: tuple[SentenceGapFinding, ...] = ()
    phase1_brief: dict[str, str] | None = None


@dataclass(frozen=True)
class ChunkNode:
    id_: str
    text: str
    metadata: dict[str, Any]

    @property
    def node_id(self) -> str:
        return self.id_


@dataclass(frozen=True)
class ScoredChunk:
    node: ChunkNode
    score: float


def _policy_chunk_node(chunk: DocumentChunk, policy: Policy) -> ChunkNode:
    return ChunkNode(
        id_=chunk.id,
        text=chunk.content,
        metadata={
            'policy_id': policy.id,
            'policy_name': policy.name,
            'policy_version': policy.version,
            'chunk_index': chunk.chunk_index,
        },
    )


class PolicyChunkPgVectorStore:
    """Read-only similarity search over the org's active internal-policy chunks."""

    def __init__(self, *, org_id: str, db: Session) -> None:
        self.org_id = org_id
        self.db = db

    def query(self, query_embedding: list[float], *, top_k: int) -> list[ScoredChunk]:
        if not query_embedding:
            return []

        distance = DocumentChunk.embedding.cosine_distance(query_embedding)
        records = (
            self.db.query(DocumentChunk, Policy, distance.label('distance'))
            .join(Policy, Policy.id == DocumentChunk.policy_id)
            .filter(
                DocumentChunk.org_id == self.org_id,
                DocumentChunk.policy_id.is_not(None),
                DocumentChunk.embedding.is_not(None),
                or_(
                    DocumentChunk.document_type.in_(tuple(LEGACY_INTERNAL_POLICY_TYPES)),
                    DocumentChunk.document_type.is_(None),
                ),
                Policy.org_id == self.org_id,
                Policy.status == 'ACTIVE',
                Policy.type != REGULATORY_REFERENCE_TYPE,
            )
            .order_by(distance.asc())
            .limit(top_k)
            .all()
        )
        return [
            ScoredChunk(
                node=_policy_chunk_node(chunk, policy),
                score=max(-1.0, min(1.0, 1.0 - float(raw_distance))),
            )
            for chunk, policy, raw_distance in records
        ]


async def run_gap_analysis(payload: AgentRunRequest) -> AgentRunResponse:
    """Run the Phase 5 workflow for one publication or business-change trigger."""
    run_id = str(uuid4())
    token = set_tenant_org_id(payload.org_id)
    try:
        async with background_db_slot():
            async with langfuse_workflow_trace(
                'gap-analysis',
                session_id=run_id,
                user_id=payload.org_id,
                tags=normalize_trace_tags('gap-analysis', payload.trigger_type),
                metadata={
                    'run_id': run_id,
                    'trigger_type': payload.trigger_type,
                    'trigger_id': payload.trigger_id or '',
                },
                input_data={
                    'trigger_type': payload.trigger_type,
                    'trigger_id': payload.trigger_id,
                    'org_id': payload.org_id,
                },
            ):
                return await _run_gap_analysis_impl(payload, run_id)
    finally:
        reset_tenant_org_id(token)


async def _run_gap_analysis_impl(payload: AgentRunRequest, run_id: str) -> AgentRunResponse:
    if SessionLocal is None:
        return AgentRunResponse(
            run_id=run_id,
            status='skipped',
            message='Gap analysis requires a configured database.',
        )

    try:
        # Short read-only load — never await embeddings/LLM while this session is open.
        with _analysis_session_scope(commit=False) as db:
            trigger = load_trigger(db, payload)
            mandatory_policy_ids = resolve_mandatory_policy_ids(db, trigger, payload)
            trigger = _detach_trigger_context(trigger)

        settings = get_settings()
        if mandatory_policy_ids is not None:
            candidates = await mandatory_policy_candidates(
                None,
                trigger.org_id,
                mandatory_policy_ids,
                trigger,
                settings,
            )
        else:
            candidates = await retrieve_policy_candidates(None, trigger, settings)

        with _analysis_session_scope(commit=False) as db:
            if (
                mandatory_policy_ids is None
                and trigger.trigger_type == 'publication'
                and trigger.publication is not None
            ):
                candidates = ensure_publication_regulator_candidates(
                    db,
                    trigger.org_id,
                    trigger.publication.source,
                    candidates,
                )

            if not candidates:
                logger.warning(
                    'gap_analysis_no_candidates_found',
                    org_id=trigger.org_id,
                    trigger_id=trigger.trigger_id,
                    trigger_type=trigger.trigger_type,
                    mandatory_policy_ids=mandatory_policy_ids,
                )
                if mandatory_policy_ids is not None:
                    # Matched business-change policies must still produce candidates.
                    for policy_id in mandatory_policy_ids:
                        policy = (
                            db.query(Policy)
                            .filter(
                                Policy.id == policy_id,
                                Policy.org_id == trigger.org_id,
                                Policy.status == 'ACTIVE',
                            )
                            .one_or_none()
                        )
                        if policy is None:
                            continue
                        fallback_candidate = policy_candidate_from_policy(
                            db,
                            policy,
                            retrieval_mode='MANDATORY_POLICY_FALLBACK',
                        )
                        if fallback_candidate is not None:
                            candidates.append(fallback_candidate)
                if not candidates and mandatory_policy_ids is None:
                    fallback_candidate = None
                    if trigger.trigger_type == 'publication' and trigger.publication is not None:
                        fallback_candidate = internal_policy_candidate_for_regulator(
                            db,
                            trigger.org_id,
                            trigger.publication.source,
                            retrieval_mode='PUBLICATION_REGULATOR_FALLBACK',
                        )
                    if fallback_candidate is None:
                        latest_policy = (
                            db.query(Policy)
                            .filter(
                                Policy.org_id == trigger.org_id,
                                Policy.status == 'ACTIVE',
                                Policy.type != REGULATORY_REFERENCE_TYPE,
                            )
                            .order_by(Policy.updated_at.desc())
                            .first()
                        )
                        if latest_policy is not None:
                            fallback_candidate = policy_candidate_from_policy(
                                db,
                                latest_policy,
                                retrieval_mode='FORCED_FALLBACK',
                            )
                    if fallback_candidate is not None:
                        logger.info(
                            'gap_analysis_using_fallback_policy',
                            policy_id=fallback_candidate.policy_id,
                            policy_name=fallback_candidate.policy_name,
                        )
                        candidates = [fallback_candidate]

            if not candidates:
                return AgentRunResponse(
                    run_id=run_id,
                    status='skipped',
                    message='No active policies found for this organization to analyze against.',
                )

            # Always analyze and overwrite the one gap row per Internal policy name.
            # ``force_reanalysis`` is accepted for API compatibility but ignored for skip.
            if payload.policy_ids and mandatory_policy_ids is None:
                allowed = set(payload.policy_ids)
                candidates = [candidate for candidate in candidates if candidate.policy_id in allowed]
            context_excerpts: list[dict[str, str]] = []
            if trigger.trigger_type == 'business_change':
                context_excerpts = retrieve_context_excerpts(db, trigger.org_id)
            publication_id = trigger.publication.id if trigger.publication is not None else None
            business_change_id = (
                trigger.business_change.id if trigger.business_change is not None else None
            )

        llm = optional_llm_provider(
            trigger.org_id, 'analysis', platform_model=settings.resolved_anthropic_gap_analysis_model)
        business_change_government_excerpts: list[GovernmentExcerpt] = []
        if trigger.trigger_type == 'business_change':
            # db=None so embedding awaits do not pin a pool connection.
            business_change_government_excerpts = await retrieve_government_excerpts_for_business_change(
                None,
                trigger.org_id,
                trigger.text,
                settings,
            )
        created = 0
        created_gap_ids: list[str] = []
        material_gap_ids: list[str] = []
        for candidate in candidates:
            internal_policy_text = '\n\n'.join(chunk.content for chunk in candidate.chunks)
            regulator_similarity, policy_government_excerpts = await asyncio.gather(
                compare_internal_policy_to_regulator_documents_with_fallback(
                    None,
                    org_id=trigger.org_id,
                    internal_policy_id=candidate.policy_id,
                    internal_policy_name=candidate.policy_name,
                    internal_policy_text=internal_policy_text,
                    settings=settings,
                ),
                retrieve_government_excerpts_for_internal_policy(
                    None,
                    trigger.org_id,
                    internal_policy_name=candidate.policy_name,
                    internal_policy_text=internal_policy_text,
                    trigger_text=trigger.text,
                    settings=settings,
                ),
            )
            if trigger.trigger_type == 'business_change':
                government_excerpts = merge_government_excerpts(
                    business_change_government_excerpts,
                    policy_government_excerpts,
                )[:MAX_BUSINESS_CHANGE_GOVERNMENT_EXCERPTS]
            else:
                government_excerpts = policy_government_excerpts[:MAX_GAP_GOVERNMENT_EXCERPTS]

            # Same-policy prior CCO judgment only (privacy-scoped); never mix other policies.
            candidate_context = list(context_excerpts)
            try:
                from app.services.compliance_history_service import (
                    retrieve_prior_cco_judgment_excerpts,
                )

                with _analysis_session_scope(commit=False) as scoped_db:
                    prior_judgment = retrieve_prior_cco_judgment_excerpts(
                        scoped_db,
                        trigger.org_id,
                        candidate.policy_id,
                        limit=MAX_PRIOR_CCO_JUDGMENT_CONTEXT,
                    )
                if prior_judgment:
                    candidate_context = [
                        *prior_judgment,
                        *candidate_context,
                    ][: MAX_PRIOR_CCO_JUDGMENT_CONTEXT + MAX_GAP_CONTEXT_EXCERPTS]
            except Exception as exc:
                logger.warning(
                    'prior_cco_judgment_context_failed',
                    policy_id=candidate.policy_id,
                    error_type=type(exc).__name__,
                )

            finding = await identify_gap(
                trigger,
                candidate,
                settings,
                llm,
                candidate_context,
                government_excerpts,
            )
            finding = calibrate_business_change_impact(trigger, candidate, finding)
            finding = calibrate_regulator_scoped_impact(
                trigger,
                candidate,
                finding,
                government_excerpts,
            )
            logger.info(
                'gap_analysis_finding_ready',
                org_id=trigger.org_id,
                policy_id=candidate.policy_id,
                trigger_type=trigger.trigger_type,
                impact_score=finding.impact_score,
                sentence_finding_count=len(finding.sentence_findings),
                government_excerpt_count=len(government_excerpts),
                government_regulators=sorted(
                    {excerpt.regulator_source for excerpt in government_excerpts}
                ),
                analysis_mode=finding.analysis_mode,
                analysis_model=finding.analysis_model,
            )
            retrieval_metadata = {
                'retrieval_mode': candidate.retrieval_mode,
                'regulator_scope': list(REGULATOR_SOURCES),
                'embedding_model': embedding_model_label(trigger.org_id, settings),
                'chunk_ids': [chunk.id for chunk in candidate.chunks],
                'chunk_scores': [round(chunk.similarity, 6) for chunk in candidate.chunks],
                'government_reference_excerpts': [
                    excerpt.as_prompt_dict() for excerpt in government_excerpts
                ],
                'government_chunk_ids': [excerpt.chunk_id for excerpt in government_excerpts],
                'regulator_document_similarity': [
                    item.as_metadata_dict() for item in regulator_similarity
                ],
                SENTENCE_FINDINGS_METADATA_KEY: [
                    {
                        'sentence': item.sentence,
                        'rule': item.rule,
                        'gap': item.gap,
                        'fix': item.fix,
                        'impact_score': item.impact_score,
                    }
                    for item in finding.sentence_findings
                ],
                **(
                    {PHASE1_BRIEF_METADATA_KEY: finding.phase1_brief}
                    if finding.phase1_brief
                    else {}
                ),
                **(
                    {
                        'business_change_government_excerpts': [
                            excerpt.as_prompt_dict()
                            for excerpt in business_change_government_excerpts
                        ],
                    }
                    if trigger.trigger_type == 'business_change'
                    else {}
                ),
            }
            if trigger.trigger_type == 'business_change':
                from app.services.business_change_policy_workflow_service import (
                    BUSINESS_CHANGE_POLICY_MATCH_KEY,
                    NEW_POLICY_FROM_BUSINESS_CHANGE_KEY,
                    REDLINE_ELIGIBLE_KEY,
                )

                retrieval_metadata[BUSINESS_CHANGE_POLICY_MATCH_KEY] = True
                retrieval_metadata[NEW_POLICY_FROM_BUSINESS_CHANGE_KEY] = False
                retrieval_metadata[REDLINE_ELIGIBLE_KEY] = True
            with _analysis_session_scope(org_id=trigger.org_id) as scoped_db:
                gap_id = persist_gap_analysis_result(
                    scoped_db,
                    org_id=trigger.org_id,
                    publication_id=publication_id,
                    business_change_id=business_change_id,
                    trigger_type=trigger.trigger_type,
                    trigger_id=trigger.trigger_id,
                    policy_id=candidate.policy_id,
                    finding=finding,
                    retrieval_metadata=retrieval_metadata,
                    force_update=True,
                )
            created += 1
            created_gap_ids.append(gap_id)
            if finding.impact_score in {'HIGH', 'MEDIUM'}:
                material_gap_ids.append(gap_id)

        if created_gap_ids:
            from app.services.compliance_history_service import backfill_missing_compliance_history

            with _analysis_session_scope() as scoped_db:
                backfill_missing_compliance_history(
                    scoped_db,
                    org_id=trigger.org_id,
                    gap_ids=created_gap_ids,
                )

        if publication_id is not None:
            with _analysis_session_scope() as scoped_db:
                publication = (
                    scoped_db.query(RegulatoryPublication)
                    .filter(RegulatoryPublication.id == publication_id)
                    .one_or_none()
                )
                if publication is not None:
                    publication.processed = True

        if settings.auto_generate_compliance_report and material_gap_ids:
            try:
                from app.services.compliance_report_service import generate_compliance_report

                # Open a short-lived session for the report; avoid holding one around other work.
                with _analysis_session_scope() as scoped_db:
                    await generate_compliance_report(
                        trigger.org_id,
                        scoped_db,
                        raise_on_failure=False,
                    )
            except Exception as exc:
                logger.warning(
                    'compliance_report_auto_generation_failed',
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                )

        # LLM analysis documents are no longer generated; review uses Request Review → redline.

        status = 'completed' if created else 'failed'
        if mandatory_policy_ids is not None and created < len(candidates):
            logger.warning(
                'gap_analysis_incomplete_mandatory_run',
                org_id=trigger.org_id,
                trigger_type=trigger.trigger_type,
                trigger_id=trigger.trigger_id,
                expected=len(candidates),
                created=created,
            )
        message = (
            f'Persisted {created} policy gap analysis result(s) for CCO review.'
            if created
            else (
                'No policy gap analyses were persisted for this mandatory trigger.'
                if mandatory_policy_ids is not None
                else 'No policy gap analyses were persisted for this trigger.'
            )
        )
        trace_event(
            'gap_analysis.completed',
            {
                'run_id': run_id,
                'org_id': trigger.org_id,
                'created_count': created,
                'retrieval_modes': sorted({candidate.retrieval_mode for candidate in candidates}),
            },
        )
        return AgentRunResponse(run_id=run_id, status=status, message=message)
    except ValueError as exc:
        return AgentRunResponse(run_id=run_id, status='skipped', message=str(exc))
    except SQLAlchemyError:
        raise


def _should_auto_generate_redlines(payload: AgentRunRequest) -> bool:
    """Deprecated: redlines are only created after an explicit review request."""
    del payload
    return False


def load_latest_revisions_for_gaps(
    db: Session,
    org_id: str,
    gap_ids: list[str],
) -> dict[str, PolicyRevision]:
    """Load the latest open review/upload workflow for each gap."""
    if not gap_ids:
        return {}
    revisions = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.org_id == org_id,
            PolicyRevision.gap_analysis_id.in_(gap_ids),
            PolicyRevision.status.in_(('PENDING', 'APPROVED')),
        )
        .order_by(PolicyRevision.created_at.desc())
        .all()
    )
    latest_by_gap: dict[str, PolicyRevision] = {}
    for revision in revisions:
        is_in_review = (
            revision.status == 'PENDING'
            and revision.review_requested_at is not None
        )
        is_upload_pending = (
            revision.status == 'APPROVED'
            and revision.reuploaded_at is None
        )
        if not is_in_review and not is_upload_pending:
            continue
        if revision.gap_analysis_id not in latest_by_gap:
            latest_by_gap[revision.gap_analysis_id] = revision
    return latest_by_gap


def gap_workflow_status(revision: PolicyRevision | None) -> str:
    """Derive the CCO-facing gap status from its review/upload workflow."""
    if revision is None:
        return 'ACTIVE'
    if revision.status == 'APPROVED' and revision.reuploaded_at is None:
        return 'UPLOAD_PENDING'
    if revision.status == 'PENDING' and revision.review_requested_at is not None:
        return 'IN_REVIEW'
    return 'ACTIVE'


def gap_analysis_ids_cleared_by_reupload(db: Session, org_id: str) -> set[str]:
    """Hide approved+reuploaded gaps until a later Policy Update overwrite.

    Overwriting the same row bumps ``created_at``, so a fresh upload analysis
    reappears in Internal Policy Gaps instead of staying hidden.
    """
    rows = (
        db.query(PolicyRevision.gap_analysis_id, PolicyRevision.reuploaded_at, GapAnalysis.created_at)
        .join(GapAnalysis, GapAnalysis.id == PolicyRevision.gap_analysis_id)
        .filter(
            PolicyRevision.org_id == org_id,
            GapAnalysis.org_id == org_id,
            PolicyRevision.status == 'APPROVED',
            PolicyRevision.reuploaded_at.is_not(None),
            PolicyRevision.gap_analysis_id.is_not(None),
        )
        .all()
    )
    cleared: set[str] = set()
    for gap_id, reuploaded_at, created_at in rows:
        if not gap_id:
            continue
        if reuploaded_at is not None and created_at is not None:
            uploaded = reuploaded_at.replace(tzinfo=None) if getattr(reuploaded_at, 'tzinfo', None) else reuploaded_at
            analyzed = created_at.replace(tzinfo=None) if getattr(created_at, 'tzinfo', None) else created_at
            if analyzed > uploaded:
                continue
        cleared.add(str(gap_id))
    return cleared


def load_trigger(db: Session, payload: AgentRunRequest) -> TriggerContext:
    trigger_type = payload.trigger_type.strip().lower()
    if trigger_type == 'publication':
        if not payload.org_id:
            raise ValueError(
                'publication triggers require an organization ID.')
        publication = (
            db.query(RegulatoryPublication)
            .filter(RegulatoryPublication.id == payload.trigger_id)
            .one_or_none()
        )
        if publication is None:
            raise ValueError('Regulatory publication was not found.')
        return TriggerContext(
            org_id=payload.org_id,
            trigger_type='publication',
            trigger_id=publication.id,
            publication=publication,
            business_change=None,
            text=publication.raw_text or publication.title,
            citation_source=publication.source,
            citation_reference=publication.url,
            label=publication.title,
        )
    if trigger_type == 'business_change':
        change = (
            db.query(BusinessChange)
            .filter(BusinessChange.id == payload.trigger_id)
            .one_or_none()
        )
        if change is None:
            raise ValueError('Business change was not found.')
        if payload.org_id and payload.org_id != change.org_id:
            raise ValueError(
                'Business change does not belong to the requested organization.')
        trigger_text = (change.event_summary or change.description).strip()
        if change.event_summary and change.description.strip():
            trigger_text = (
                f'{change.event_summary.strip()}\n\n'
                f'Original change description: {change.description.strip()}'
            )
        return TriggerContext(
            org_id=change.org_id,
            trigger_type='business_change',
            trigger_id=change.id,
            publication=None,
            business_change=change,
            text=trigger_text,
            citation_source='Business change',
            citation_reference=f'Business change {change.id} ({change.change_type})',
            label=f'{change.change_type.replace("_", " ").title()}: {change.description[:120]}',
        )
    if trigger_type == 'policy_update':
        if not payload.org_id:
            raise ValueError('policy_update triggers require an organization ID.')
        policy = (
            db.query(Policy)
            .filter(Policy.id == payload.trigger_id, Policy.org_id == payload.org_id)
            .one_or_none()
        )
        if policy is None:
            raise ValueError('Uploaded policy was not found.')
        policy_chunks = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.policy_id == policy.id)
            .order_by(DocumentChunk.chunk_index)
            .limit(MAX_CHUNKS_PER_POLICY)
            .all()
        )
        excerpt = '\n\n'.join(chunk.content for chunk in policy_chunks)
        return TriggerContext(
            org_id=payload.org_id,
            trigger_type='policy_update',
            trigger_id=policy.id,
            publication=None,
            business_change=None,
            text=(
                f'Policy library update: {policy.name} version {policy.version} was uploaded. '
                'Review the policy text for regulatory compliance gaps using firm context and '
                f'retrieved excerpts.\n\n{excerpt or policy.name}'
            ),
            citation_source='Policy library',
            citation_reference=f'Policy upload {policy.name} v{policy.version}',
            label=f'{policy.name} v{policy.version}',
        )
    raise ValueError('trigger_type must be publication, business_change, or policy_update.')


def resolve_mandatory_policy_ids(
    db: Session,
    trigger: TriggerContext,
    payload: AgentRunRequest,
) -> list[str] | None:
    """Return policy IDs that must receive a gap row for business changes and policy uploads."""
    if trigger.trigger_type == 'business_change':
        # Prefer explicitly matched policy IDs from the business-change workflow.
        if payload.policy_ids:
            return list(payload.policy_ids)
        return [
            policy_id
            for (policy_id,) in db.query(Policy.id)
            .filter(
                Policy.org_id == trigger.org_id,
                Policy.status == 'ACTIVE',
                Policy.type != REGULATORY_REFERENCE_TYPE,
            )
            .order_by(Policy.name.asc())
            .all()
        ]
    if trigger.trigger_type == 'policy_update':
        return [trigger.trigger_id]
    if payload.policy_ids:
        return list(payload.policy_ids)
    return None


async def mandatory_policy_candidates(
    db: Session | None,
    org_id: str,
    policy_ids: list[str],
    trigger: TriggerContext,
    settings: Settings,
) -> list[PolicyCandidate]:
    """Build one candidate per required policy for business_change and policy_update runs.

    Prefer ``db=None`` so semantic embedding awaits do not pin a pool connection.
    """
    if not policy_ids:
        return []

    def _load_policies(session: Session) -> list[Policy]:
        return (
            session.query(Policy)
            .filter(
                Policy.org_id == org_id,
                Policy.status == 'ACTIVE',
                Policy.id.in_(policy_ids),
            )
            .order_by(Policy.name.asc())
            .all()
        )

    if db is not None:
        policies = _load_policies(db)
    else:
        with _analysis_session_scope(commit=False) as scoped_db:
            policies = _load_policies(scoped_db)
            # Detach so policy objects remain usable after the session closes.
            for policy in policies:
                eager_load_columns(policy)
                scoped_db.expunge(policy)

    if not policies:
        return []

    semantic_chunks_by_policy: dict[str, list[RetrievedChunk]] = {}
    # Always embed without holding the caller's session.
    try:
        semantic_candidates = await retrieve_policy_candidates(None, trigger, settings)
        for candidate in semantic_candidates:
            if candidate.policy_id in policy_ids:
                semantic_chunks_by_policy[candidate.policy_id] = candidate.chunks
    except Exception as exc:
        logger.warning(
            'gap_analysis_mandatory_semantic_retrieval_failed',
            error_type=type(exc).__name__,
        )

    candidates: list[PolicyCandidate] = []
    for policy in policies:
        if not is_internal_policy_type(policy.type):
            continue
        chunks = semantic_chunks_by_policy.get(policy.id)
        retrieval_mode = 'MANDATORY_POLICY_SEMANTIC'
        if not chunks:
            def _load_chunks(session: Session) -> list[RetrievedChunk]:
                policy_chunks = (
                    session.query(DocumentChunk)
                    .filter(DocumentChunk.policy_id == policy.id)
                    .order_by(DocumentChunk.chunk_index)
                    .limit(MAX_CHUNKS_PER_POLICY)
                    .all()
                )
                return [
                    RetrievedChunk(
                        id=chunk.id,
                        index=chunk.chunk_index,
                        content=chunk.content,
                        similarity=0.0,
                    )
                    for chunk in policy_chunks
                ]

            if db is not None:
                chunks = _load_chunks(db)
            else:
                with _analysis_session_scope(commit=False) as scoped_db:
                    chunks = _load_chunks(scoped_db)
            retrieval_mode = 'MANDATORY_POLICY_CHUNKS'
        if not chunks:
            logger.warning(
                'gap_analysis_mandatory_policy_missing_chunks',
                policy_id=policy.id,
                policy_name=policy.name,
            )
            continue
        candidates.append(
            PolicyCandidate(
                policy_id=policy.id,
                policy_name=policy.name,
                policy_version=policy.version,
                chunks=chunks,
                retrieval_mode=retrieval_mode,
            )
        )
    return candidates


async def retrieve_policy_candidates(
    db: Session | None,
    trigger: TriggerContext,
    settings: Settings,
) -> list[PolicyCandidate]:
    """Embed the trigger and retrieve tenant-scoped policy chunks via pgvector.

    Prefer ``db=None`` so embedding awaits do not pin a pool connection. When a session is
    passed, it is only used for synchronous queries around the await.
    """
    embedder = optional_embedding_provider(trigger.org_id)

    def _has_embeds(session: Session) -> bool:
        return embedder is not None and has_embedded_policy_chunks(session, trigger.org_id)

    if db is not None:
        use_semantic = _has_embeds(db)
    else:
        with _analysis_session_scope(commit=False) as scoped_db:
            use_semantic = _has_embeds(scoped_db)

    if use_semantic and embedder is not None:
        try:
            # Embedding must not hold a SQLAlchemy connection (can take seconds–minutes).
            embedded = await embedder.embed([trigger.text], trace_name='gap_analysis.query')
            trigger_embedding = embedded.vectors[0]

            def _vector_candidates(session: Session) -> list[PolicyCandidate]:
                vector_store = PolicyChunkPgVectorStore(org_id=trigger.org_id, db=session)
                matches = vector_store.query(trigger_embedding, top_k=MAX_VECTOR_RESULTS)
                return candidates_from_nodes(matches, 'SEMANTIC_PGVECTOR')

            if db is not None:
                candidates = _vector_candidates(db)
            else:
                with _analysis_session_scope(commit=False) as scoped_db:
                    candidates = _vector_candidates(scoped_db)
            if candidates:
                return candidates
        # Provider and database failures retain a local development fallback.
        except Exception as exc:
            logger.warning(
                'gap_analysis_semantic_retrieval_failed',
                error_type=type(exc).__name__,
            )
            if settings.is_production:
                raise ValueError(
                    'Semantic policy retrieval failed; retry the analysis.') from exc

    if db is not None:
        return lexical_policy_candidates(db, trigger)
    with _analysis_session_scope(commit=False) as scoped_db:
        return lexical_policy_candidates(scoped_db, trigger)


def has_embedded_policy_chunks(db: Session, org_id: str) -> bool:
    """Avoid provider calls when this tenant has no pgvector-ready policy chunks."""
    return (
        db.query(DocumentChunk.id)
        .join(Policy, Policy.id == DocumentChunk.policy_id)
        .filter(
            DocumentChunk.org_id == org_id,
            DocumentChunk.policy_id.is_not(None),
            DocumentChunk.embedding.is_not(None),
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .limit(1)
        .first()
        is not None
    )


def candidates_from_nodes(matches: Sequence[Any], retrieval_mode: str) -> list[PolicyCandidate]:
    grouped: dict[str, list[RetrievedChunk]] = defaultdict(list)
    details: dict[str, tuple[str, int]] = {}
    for match in matches:
        node = getattr(match, 'node', None)
        metadata = getattr(node, 'metadata', {}) if node is not None else {}
        policy_id = str(metadata.get('policy_id') or '')
        if not policy_id or len(grouped[policy_id]) >= MAX_CHUNKS_PER_POLICY:
            continue
        details[policy_id] = (
            str(metadata.get('policy_name') or 'Untitled policy'),
            int(metadata.get('policy_version') or 1),
        )
        grouped[policy_id].append(
            RetrievedChunk(
                id=str(getattr(node, 'node_id', '')
                       or getattr(node, 'id_', '')),
                index=int(metadata.get('chunk_index') or 0),
                content=str(getattr(node, 'text', '')),
                similarity=float(getattr(match, 'score', 0.0) or 0.0),
            )
        )

    candidates = [
        PolicyCandidate(
            policy_id=policy_id,
            policy_name=details[policy_id][0],
            policy_version=details[policy_id][1],
            chunks=chunks,
            retrieval_mode=retrieval_mode,
        )
        for policy_id, chunks in grouped.items()
    ]
    return sorted(
        candidates,
        key=lambda candidate: max(
            chunk.similarity for chunk in candidate.chunks),
        reverse=True,
    )[:MAX_POLICIES_PER_RUN]


def lexical_policy_candidates(db: Session, trigger: TriggerContext) -> list[PolicyCandidate]:
    """Local-development fallback when an embedding provider is intentionally absent."""
    query_terms = meaningful_terms(trigger.text)
    if not query_terms:
        return []
    records = (
        db.query(DocumentChunk, Policy)
        .join(Policy, Policy.id == DocumentChunk.policy_id)
        .filter(
            DocumentChunk.org_id == trigger.org_id,
            DocumentChunk.policy_id.is_not(None),
            Policy.org_id == trigger.org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .all()
    )
    ranked: list[tuple[DocumentChunk, Policy, float]] = []
    for chunk, policy in records:
        text = chunk.content.lower()
        matched = sum(1 for term in query_terms if term in text)
        if matched:
            ranked.append((chunk, policy, matched / len(query_terms)))
    ranked.sort(key=lambda record: record[2], reverse=True)
    matches = [
        ScoredChunk(node=_policy_chunk_node(chunk, policy), score=similarity)
        for chunk, policy, similarity in ranked[:MAX_VECTOR_RESULTS]
    ]
    return candidates_from_nodes(matches, 'LEXICAL_FALLBACK')


async def identify_gap(
    trigger: TriggerContext,
    candidate: PolicyCandidate,
    settings: Settings,
    llm: LLMProvider | None,
    context_excerpts: list[dict[str, str]] | None = None,
    government_excerpts: list[GovernmentExcerpt] | None = None,
) -> GapFinding:
    if llm is None:
        return fallback_gap(trigger, candidate)
    prompt_payload: dict[str, Any] = {
        'analysis_workflow': internal_policy_analysis_workflow(),
        'trigger': {
            'type': trigger.trigger_type,
            'source': trigger.citation_source,
            'reference': trigger.citation_reference,
            'text': trigger.text[:MAX_GAP_ANALYSIS_TRIGGER_CHARS],
        },
        'policy': {
            'id': candidate.policy_id,
            'name': candidate.policy_name,
            'version': candidate.policy_version,
            'type': INTERNAL_POLICY_TYPE,
            'regulator_scope': list(REGULATOR_SOURCES),
            'retrieved_excerpts': [
                {
                    'chunk_id': chunk.id,
                    'chunk_index': chunk.index,
                    'text': chunk.content[:MAX_CHUNK_CONTEXT_CHARS],
                }
                for chunk in candidate.chunks
            ],
        },
        'government_reference_excerpts': [
            {
                **excerpt.as_prompt_dict(),
                'text': excerpt.content[:MAX_GAP_GOVERNMENT_EXCERPT_CHARS],
            }
            for excerpt in (government_excerpts or [])[:MAX_GAP_GOVERNMENT_EXCERPTS]
        ],
        'firm_context_excerpts': [
            {
                **item,
                'text': item.get('text', '')[:MAX_GAP_CONTEXT_EXCERPT_CHARS],
            }
            for item in (context_excerpts or [])[
                : MAX_PRIOR_CCO_JUDGMENT_CONTEXT + MAX_GAP_CONTEXT_EXCERPTS
            ]
        ],
    }
    if trigger.business_change is not None:
        prompt_payload['business_change'] = {
            'id': trigger.business_change.id,
            'change_type': trigger.business_change.change_type,
            'description': trigger.business_change.description[:3_000],
            'event_summary': (trigger.business_change.event_summary or '')[:1_500],
            'requires_policy_review': bool(trigger.business_change.triggers_review),
        }
    prompt = json.dumps(prompt_payload, ensure_ascii=True)
    system_prompt = (
        BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT
        if trigger.trigger_type == 'business_change'
        else GAP_ANALYSIS_SYSTEM_PROMPT
    )
    gap_model = llm.model_label
    last_error: Exception | None = None
    truncation_retry_hint = ''
    best_truncated: GapFinding | None = None
    for attempt in range(1, settings.anthropic_rate_limit_max_attempts + 1):
        try:
            user_content = prompt + truncation_retry_hint
            response = await llm.generate(
                [ChatMessage('user', user_content)],
                max_tokens=GAP_ANALYSIS_MAX_TOKENS,
                system=system_prompt,
                temperature=0,
                cache_system=True,
                throttle='opus',
                trace_name='gap_analysis.llm',
                trace_metadata={
                    'policy_id': candidate.policy_id,
                    'trigger_type': trigger.trigger_type,
                    'trigger_id': trigger.trigger_id,
                },
            )
            data = response_json(response)
            sentence_findings = parse_sentence_findings(data)
            phase1_brief = parse_phase1_brief(data)
            if not phase1_brief:
                logger.warning(
                    'gap_analysis_missing_phase1_brief',
                    policy_id=candidate.policy_id,
                    trigger_type=trigger.trigger_type,
                    model=gap_model,
                )
            summary = summarize_sentence_findings(sentence_findings, data, candidate)
            if summary or sentence_findings:
                overall_impact = normalize_impact_score(data.get('impact_score'))
                if sentence_findings:
                    overall_impact = highest_impact_score(
                        [item.impact_score for item in sentence_findings] + [overall_impact]
                    )
                elif overall_impact in {'HIGH', 'MEDIUM'} and not sentence_findings:
                    overall_impact = 'INFO'
                first = sentence_findings[0] if sentence_findings else None
                finding = GapFinding(
                    impact_score=overall_impact,
                    summary=summary[:MAX_SUMMARY_CHARS],
                    citations=normalize_citations(data.get('citations'), trigger),
                    analysis_mode='LLM',
                    analysis_model=gap_model,
                    rule=(first.rule if first else extract_optional_finding_text(data, 'rule', 'rules')),
                    fix=(first.fix if first else extract_optional_finding_text(data, 'fix', 'remediation')),
                    sentence_findings=sentence_findings,
                    phase1_brief=phase1_brief,
                )
                if response.truncated and len(sentence_findings) <= 1:
                    logger.warning(
                        'gap_analysis_response_truncated',
                        policy_id=candidate.policy_id,
                        model=gap_model,
                        sentence_finding_count=len(sentence_findings),
                        attempt=attempt,
                    )
                    # Retry only when the cut-off left us with an empty or single finding.
                    if (
                        best_truncated is None
                        or len(sentence_findings) > len(best_truncated.sentence_findings)
                    ):
                        best_truncated = finding
                    if attempt < settings.anthropic_rate_limit_max_attempts:
                        truncation_retry_hint = GAP_ANALYSIS_TRUNCATION_RETRY_HINT
                        continue
                return finding
            last_error = ValueError('Model response did not include sentence findings or a summary.')
        except Exception as exc:
            last_error = exc
            logger.warning(
                'gap_analysis_llm_failed',
                error_type=type(exc).__name__,
                error=str(exc)[:500],
                model=gap_model,
                policy_id=candidate.policy_id,
                attempt=attempt,
                rate_limited=is_rate_limit_error(exc),
                billing_blocked=is_billing_error(exc),
            )
            if is_non_retryable_error(exc):
                break
            if attempt < settings.anthropic_rate_limit_max_attempts:
                delay = retry_delay(exc, attempt, settings)
                if delay > 0:
                    await asyncio.sleep(delay)
    if best_truncated is not None:
        return best_truncated
    return llm_failure_gap(trigger, candidate, settings, last_error, model=gap_model)


def fallback_gap(trigger: TriggerContext, candidate: PolicyCandidate) -> GapFinding:
    """Deterministic placeholder when no AI model is configured."""
    text = trigger.text.lower()
    high_signal = ('must', 'shall', 'require', 'required',
                   'prohibited', 'enforcement', 'violation')
    score = 'HIGH' if any(word in text for word in high_signal) else 'MEDIUM'
    return GapFinding(
        impact_score=score,
        summary=(
            f'Automated gap analysis is unavailable for {candidate.policy_name} '
            f'(version {candidate.policy_version}). Configure an AI model and re-run analysis '
            f'against trigger "{trigger.label}".'
        ),
        citations=normalize_citations([], trigger),
        analysis_mode='RULE_FALLBACK',
        analysis_model=None,
    )


def llm_failure_gap(
    trigger: TriggerContext,
    candidate: PolicyCandidate,
    settings: Settings,
    error: Exception | None,
    *,
    model: str | None = None,
) -> GapFinding:
    """Preserve a visible failure state instead of a misleading template summary."""
    user_message = user_error_message(error)
    if is_billing_error(error):
        summary = (
            f'Gap analysis for {candidate.policy_name} (version {candidate.policy_version}) '
            f'could not run because {provider_label(error)} credits are exhausted. {user_message}'
        )
        analysis_mode = 'LLM_BILLING_BLOCKED'
    else:
        summary = (
            f'Gap analysis for {candidate.policy_name} (version {candidate.policy_version}) '
            f'failed. Re-run analysis from the Gap Analysis page after resolving the issue. '
            f'{user_message}'
        )
        analysis_mode = 'LLM_FAILED'
    return GapFinding(
        impact_score='MEDIUM',
        summary=summary[:MAX_SUMMARY_CHARS],
        citations=normalize_citations([], trigger),
        analysis_mode=analysis_mode,
        analysis_model=model or settings.resolved_anthropic_gap_analysis_model,
    )


def extract_plain_english_summary(data: dict[str, Any]) -> str:
    for key in ('gap', 'plain_english_summary', 'summary', 'gap_summary', 'analysis'):
        value = str(data.get(key) or '').strip()
        if value:
            return value
    return ''


def _normalize_finding_key_text(value: str) -> str:
    return ' '.join(value.lower().split())


def highest_impact_score(scores: Sequence[str]) -> str:
    rank = {'HIGH': 0, 'MEDIUM': 1, 'INFO': 2, 'NONE': 3}
    normalized = [normalize_impact_score(score) for score in scores]
    if not normalized:
        return 'INFO'
    return min(normalized, key=lambda score: rank.get(score, 99))


def parse_sentence_findings(data: dict[str, Any]) -> tuple[SentenceGapFinding, ...]:
    """Parse sentence-level RULE/GAP/FIX findings and drop duplicate comment texts.

    Allows the same RULE citation on multiple findings when GAP/FIX differ (common
    for AML programs under FINRA 3310). Drops only exact duplicates of the
    (rule, gap, fix) triple or identical gap wording.
    """
    raw_findings = data.get('findings')
    if not isinstance(raw_findings, list):
        # Legacy single-finding responses still used during rollout.
        legacy_rule = extract_optional_finding_text(data, 'rule', 'rules')
        legacy_gap = extract_plain_english_summary(data)
        legacy_fix = extract_optional_finding_text(data, 'fix', 'remediation')
        if legacy_gap and (legacy_rule or legacy_fix):
            raw_findings = [
                {
                    'sentence': '',
                    'rule': legacy_rule or '',
                    'gap': legacy_gap,
                    'fix': legacy_fix or '',
                    'impact_score': data.get('impact_score') or 'MEDIUM',
                }
            ]
        else:
            return ()

    parsed: list[SentenceGapFinding] = []
    seen_gaps: set[str] = set()
    seen_triples: set[tuple[str, str, str]] = set()

    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        sentence = ' '.join(str(item.get('sentence') or '').split()).strip()
        rule = strip_markdown_emphasis(' '.join(str(item.get('rule') or '').split()))
        gap = strip_markdown_emphasis(' '.join(str(item.get('gap') or '').split()))
        fix = strip_markdown_emphasis(' '.join(str(item.get('fix') or '').split()))
        if not gap and not rule and not fix:
            continue
        impact = normalize_impact_score(item.get('impact_score'))
        if impact not in {'HIGH', 'MEDIUM'}:
            impact = 'MEDIUM'
        rule_key = _normalize_finding_key_text(rule)
        gap_key = _normalize_finding_key_text(gap)
        fix_key = _normalize_finding_key_text(fix)
        triple = (rule_key, gap_key, fix_key)
        if triple in seen_triples:
            continue
        # Identical gap prose is the same finding even if the rule string varies slightly.
        if gap_key and gap_key in seen_gaps:
            continue
        seen_triples.add(triple)
        if gap_key:
            seen_gaps.add(gap_key)
        parsed.append(
            SentenceGapFinding(
                sentence=sentence[:1_000],
                rule=rule[:MAX_SUMMARY_CHARS],
                gap=gap[:MAX_SUMMARY_CHARS],
                fix=fix[:MAX_SUMMARY_CHARS],
                impact_score=impact,
            )
        )
        if len(parsed) >= MAX_SENTENCE_FINDINGS:
            break
    return tuple(parsed)


def parse_phase1_brief(data: dict[str, Any]) -> dict[str, str] | None:
    """Extract the model's forced Phase 1 understanding block when present."""
    raw = data.get('phase1_brief')
    if not isinstance(raw, dict):
        return None
    brief: dict[str, str] = {}
    for key in (
        'policy_understanding',
        'companion_understanding',
        'covered_well',
        'silent_or_thin',
    ):
        value = ' '.join(str(raw.get(key) or '').split()).strip()
        if value:
            brief[key] = value[:500]
    return brief or None


def summarize_sentence_findings(
    findings: tuple[SentenceGapFinding, ...],
    data: dict[str, Any],
    candidate: PolicyCandidate,
) -> str:
    if findings:
        return (
            f'{candidate.policy_name} has {len(findings)} regulation gap(s) requiring CCO review. '
            f'First gap: {findings[0].gap}'
        )[:MAX_SUMMARY_CHARS]
    legacy = extract_plain_english_summary(data)
    if legacy:
        return legacy
    return (
        f'No material regulation discrepancies found for {candidate.policy_name} '
        f'(version {candidate.policy_version}).'
    )


def sentence_findings_from_metadata(metadata: dict[str, Any] | None) -> list[dict[str, str]]:
    raw = (metadata or {}).get(SENTENCE_FINDINGS_METADATA_KEY)
    if not isinstance(raw, list):
        return []
    findings: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                'sentence': str(item.get('sentence') or '').strip(),
                'rule': str(item.get('rule') or '').strip(),
                'gap': str(item.get('gap') or '').strip(),
                'fix': str(item.get('fix') or '').strip(),
                'impact_score': normalize_impact_score(item.get('impact_score')),
            }
        )
    return findings


def count_gap_findings(analysis: GapAnalysis) -> int:
    """Cheap finding count for list payloads that omit full RULE/GAP/FIX bodies."""
    metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
    findings = sentence_findings_from_metadata(metadata)
    if findings:
        return len(findings)
    if (
        normalize_impact_score(analysis.impact_score) in {'HIGH', 'MEDIUM'}
        and str(analysis.gap_summary or '').strip()
    ):
        return 1
    return 0


def build_gap_finding_summaries(analysis: GapAnalysis) -> list[GapFindingSummary]:
    """Build dropdown items from sentence findings, falling back to the row summary."""
    metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
    findings = sentence_findings_from_metadata(metadata)
    gaps: list[GapFindingSummary] = []
    for item in findings:
        summary = item.get('finding') or item.get('gap') or item.get('sentence') or ''
        if not summary:
            continue
        gaps.append(
            GapFindingSummary(
                summary=str(item.get('finding') or item.get('gap') or item.get('sentence') or ''),
                rule=item.get('rule') or None,
                fix=item.get('fix') or None,
                impact_score=normalize_impact_score(item.get('impact_score')),
                sentence=item.get('sentence') or None,
                sentence_label=str(item.get('sentence_label') or '').strip() or None,
                display_kind=item.get('display_kind') if item.get('display_kind') in {'policy', 'marketing'} else None,
                finding=str(item.get('finding') or '').strip() or None,
                why=str(item.get('why') or item.get('gap') or '').strip() or None,
                confidence=str(item.get('confidence') or '').strip() or None,
                options=[str(choice) for choice in (item.get('options') or []) if choice],
                options_display=str(item.get('options_display') or '').strip() or None,
            )
        )
    if gaps:
        return gaps
    summary = str(analysis.gap_summary or '').strip()
    if not summary:
        return []
    return [
        GapFindingSummary(
            summary=summary,
            rule=str(analysis.rule or '').strip() or None,
            fix=str(analysis.fix or '').strip() or None,
            impact_score=normalize_impact_score(analysis.impact_score),
        )
    ]


def extract_optional_finding_text(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = str(data.get(key) or '').strip()
        if value:
            return value[:MAX_SUMMARY_CHARS]
    return None


# Word comments carry the full LLM RULE / GAP / FIX text.
MAX_RULE_GAP_FIX_FIELD_CHARS = 4_000
MAX_RULE_GAP_FIX_COMMENT_CHARS = 12_000


def format_gap_rule_fix_comment(
    *,
    rule: str | None,
    gap: str | None,
    fix: str | None,
    max_rule_chars: int = MAX_RULE_GAP_FIX_FIELD_CHARS,
    max_gap_chars: int = MAX_RULE_GAP_FIX_FIELD_CHARS,
    max_fix_chars: int = MAX_RULE_GAP_FIX_FIELD_CHARS,
    max_total_chars: int = MAX_RULE_GAP_FIX_COMMENT_CHARS,
) -> str:
    """Build Word-comment text as RULE / GAP / FIX with the full LLM wording."""

    def _normalize_field(value: str | None, limit: int) -> str:
        text = ' '.join((value or '').split()).strip()
        if not text:
            return ''
        if len(text) <= limit:
            return text
        # Soft safety cap only; keep a clean word boundary without a mid-word ellipsis.
        clipped = text[:limit].rsplit(' ', 1)[0].rstrip(' ,;:-')
        return clipped or text[:limit]

    parts: list[str] = []
    rule_text = _normalize_field(rule, max_rule_chars)
    gap_text = _normalize_field(gap, max_gap_chars)
    fix_text = _normalize_field(fix, max_fix_chars)
    if rule_text:
        parts.append(f'RULE: {rule_text}')
    if gap_text:
        parts.append(f'GAP: {gap_text}')
    if fix_text:
        parts.append(f'FIX: {fix_text}')
    comment = '\n\n'.join(parts)
    if len(comment) <= max_total_chars:
        return comment
    # Prefer keeping FIX when the packed comment exceeds the hard ceiling.
    while len(comment) > max_total_chars and parts:
        longest_idx = max(range(len(parts)), key=lambda idx: len(parts[idx]))
        label, _, body = parts[longest_idx].partition(': ')
        if len(body) < 80:
            break
        trimmed_body = _normalize_field(body, max(len(body) - 200, 40))
        parts[longest_idx] = f'{label}: {trimmed_body}'
        comment = '\n\n'.join(parts)
    return comment[:max_total_chars]


def normalize_rule_gap_fix_comment(
    text: str | None,
    *,
    max_total_chars: int = MAX_RULE_GAP_FIX_COMMENT_CHARS,
) -> str:
    """Ensure RULE / GAP / FIX appear on separate lines with a blank line between them."""
    raw = (text or '').replace('\r\n', '\n').replace('\r', '\n').strip()
    if not raw:
        return ''

    # Accept either already-separated or single-line "RULE: … GAP: … FIX: …" blobs.
    match = re.search(
        r'(?is)^\s*RULE:\s*(.*?)\s*GAP:\s*(.*?)\s*FIX:\s*(.*?)\s*$',
        raw,
    )
    if match:
        return format_gap_rule_fix_comment(
            rule=match.group(1),
            gap=match.group(2),
            fix=match.group(3),
            max_total_chars=max_total_chars,
        )

    # Preserve intentional newlines when labels already start their own lines.
    lines = [line.strip() for line in raw.split('\n') if line.strip()]
    current = None
    buckets: dict[str, list[str]] = {'RULE': [], 'GAP': [], 'FIX': []}
    for line in lines:
        upper = line.upper()
        if upper.startswith('RULE:'):
            current = 'RULE'
            buckets['RULE'].append(line.split(':', 1)[1].strip())
            continue
        if upper.startswith('GAP:'):
            current = 'GAP'
            buckets['GAP'].append(line.split(':', 1)[1].strip())
            continue
        if upper.startswith('FIX:'):
            current = 'FIX'
            buckets['FIX'].append(line.split(':', 1)[1].strip())
            continue
        if current:
            buckets[current].append(line)
    if buckets['RULE'] or buckets['GAP'] or buckets['FIX']:
        return format_gap_rule_fix_comment(
            rule=' '.join(buckets['RULE']).strip(),
            gap=' '.join(buckets['GAP']).strip(),
            fix=' '.join(buckets['FIX']).strip(),
            max_total_chars=max_total_chars,
        )
    return raw[:max_total_chars]


def summarize_text_for_redline_comment(text: str | None, *, max_words: int) -> str:
    """Reduce a long finding to a short Word-comment snippet."""
    words = [part for part in ' '.join((text or '').split()).split(' ') if part]
    if not words:
        return ''
    if len(words) <= max_words:
        return ' '.join(words)
    return ' '.join(words[:max_words]).rstrip('.,;:') + '…'


def calibrate_business_change_impact(
    trigger: TriggerContext,
    candidate: PolicyCandidate,
    finding: GapFinding,
) -> GapFinding:
    """Ensure flagged business changes produce material scores for matched policies."""
    if finding.impact_score != 'INFO':
        return finding
    if trigger.trigger_type != 'business_change' or trigger.business_change is None:
        return finding
    del candidate
    summary = finding.summary
    if not finding.sentence_findings:
        summary = (
            f'{finding.summary.rstrip()} Business change "{trigger.business_change.change_type}" '
            f'matched this Internal Policy and requires CCO review.'
        ).strip()
    return GapFinding(
        impact_score='MEDIUM',
        summary=summary[:MAX_SUMMARY_CHARS],
        citations=finding.citations,
        analysis_mode=finding.analysis_mode,
        analysis_model=finding.analysis_model,
        rule=finding.rule,
        fix=finding.fix,
        sentence_findings=finding.sentence_findings,
        phase1_brief=finding.phase1_brief,
    )


def calibrate_regulator_scoped_impact(
    trigger: TriggerContext,
    candidate: PolicyCandidate,
    finding: GapFinding,
    government_excerpts: list[GovernmentExcerpt],
) -> GapFinding:
    """Avoid silent INFO results when government excerpts and sentence findings both exist.

    Never promote an empty-findings INFO result to MEDIUM — that creates misleading
    "No material regulation discrepancies" rows with a material impact badge.
    """
    del candidate
    if finding.impact_score != 'INFO':
        return finding
    if not finding.sentence_findings:
        return finding
    if trigger.trigger_type not in {'policy_update', 'publication'}:
        return finding
    if not government_excerpts:
        return finding
    if trigger.trigger_type == 'publication' and trigger.publication is not None:
        excerpt_sources = {excerpt.regulator_source for excerpt in government_excerpts}
        if trigger.publication.source.strip().upper() not in excerpt_sources:
            return finding
    return GapFinding(
        impact_score='MEDIUM',
        summary=finding.summary,
        citations=finding.citations,
        analysis_mode=finding.analysis_mode,
        analysis_model=finding.analysis_model,
        rule=finding.rule,
        fix=finding.fix,
        sentence_findings=finding.sentence_findings,
        phase1_brief=finding.phase1_brief,
    )


def policy_candidate_from_policy(
    db: Session,
    policy: Policy,
    *,
    retrieval_mode: str,
) -> PolicyCandidate | None:
    policy_chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.policy_id == policy.id)
        .order_by(DocumentChunk.chunk_index)
        .limit(MAX_CHUNKS_PER_POLICY)
        .all()
    )
    if not policy_chunks:
        return None
    return PolicyCandidate(
        policy_id=policy.id,
        policy_name=policy.name,
        policy_version=policy.version,
        chunks=[
            RetrievedChunk(
                id=chunk.id,
                index=chunk.chunk_index,
                content=chunk.content,
                similarity=0.0,
            )
            for chunk in policy_chunks
        ],
        retrieval_mode=retrieval_mode,
    )


def internal_policy_candidate_for_regulator(
    db: Session,
    org_id: str,
    regulator_source: str,
    *,
    retrieval_mode: str,
) -> PolicyCandidate | None:
    normalized_source = regulator_source.strip().upper()
    if normalized_source not in INTERNAL_REGULATOR_POLICY_NAMES:
        return None
    policy = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
            Policy.name == normalized_source,
        )
        .order_by(Policy.version.desc())
        .first()
    )
    if policy is None:
        return None
    return policy_candidate_from_policy(db, policy, retrieval_mode=retrieval_mode)


def ensure_publication_regulator_candidates(
    db: Session,
    org_id: str,
    publication_source: str,
    candidates: list[PolicyCandidate],
) -> list[PolicyCandidate]:
    """Always analyze the internal policy that matches a publication's regulator source."""
    matched = internal_policy_candidate_for_regulator(
        db,
        org_id,
        publication_source,
        retrieval_mode='PUBLICATION_REGULATOR_MATCH',
    )
    if matched is None:
        return candidates
    deduped = [candidate for candidate in candidates if candidate.policy_id != matched.policy_id]
    return [matched, *deduped][:MAX_POLICIES_PER_RUN]


def normalize_citations(value: Any, trigger: TriggerContext) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            reference = str(item.get('reference') or '').strip()
            if reference and reference != trigger.citation_reference:
                continue
            citations.append(
                {
                    'source': str(item.get('source') or trigger.citation_source).strip()[:300],
                    'reference': trigger.citation_reference,
                    'quote': str(item.get('quote') or '').strip()[:1_000],
                    'rationale': str(item.get('rationale') or '').strip()[:1_000],
                }
            )
    if not citations:
        citations = [
            {
                'source': trigger.citation_source,
                'reference': trigger.citation_reference,
                'quote': trigger.text[:1_000],
                'rationale': 'Trigger supplied to the policy-gap workflow.',
            }
        ]
    return citations


def retrieve_context_excerpts(db: Session, org_id: str, limit: int = MAX_GAP_CONTEXT_EXCERPTS) -> list[dict[str, str]]:
    """Load onboarding context document chunks to enrich gap-analysis prompts."""
    records = (
        db.query(DocumentChunk, ContextDocument)
        .join(ContextDocument, ContextDocument.id == DocumentChunk.context_document_id)
        .filter(
            DocumentChunk.org_id == org_id,
            DocumentChunk.context_document_id.is_not(None),
        )
        .order_by(DocumentChunk.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            'document_type': context.document_type,
            'title': context.title,
            'text': chunk.content[:MAX_GAP_CONTEXT_EXCERPT_CHARS],
        }
        for chunk, context in records
    ]


def meaningful_terms(text: str) -> set[str]:
    stop_words = {
        'about', 'after', 'against', 'before', 'being', 'business', 'change', 'from',
        'have', 'into', 'must', 'policy', 'that', 'the', 'their', 'this', 'with', 'would',
    }
    return {
        term
        for term in re.findall(r'[a-zA-Z][a-zA-Z0-9_-]{3,}', text.lower())
        if term not in stop_words
    }


def parse_gap_fields_loosely(text: str) -> dict[str, Any] | None:
    """Recover required gap-analysis fields when the model returns almost-valid JSON."""
    impact_match = re.search(
        r'"impact_score"\s*:\s*"(HIGH|MEDIUM|INFO|NONE|CRITICAL|LOW)"',
        text,
        re.IGNORECASE,
    )
    summary_match = re.search(
        r'"(?:gap|plain_english_summary)"\s*:\s*"(.*?)"\s*,\s*"(?:fix|citations)"\s*:',
        text,
        re.DOTALL,
    )
    if summary_match is None:
        summary_match = re.search(
            r'"plain_english_summary"\s*:\s*"(.*?)"\s*\}\s*$',
            text,
            re.DOTALL,
        )
    if summary_match is None:
        for key in ('gap', 'summary', 'gap_summary', 'analysis'):
            summary_match = re.search(
                rf'"{key}"\s*:\s*"(.*?)"\s*,\s*"(?:citations|impact_score|fix|rule)"\s*:',
                text,
                re.DOTALL,
            )
            if summary_match is not None:
                break

    summary = ''
    if summary_match is not None:
        summary = (
            summary_match.group(1)
            .replace('\\n', '\n')
            .replace('\\"', '"')
            .replace('\\\\', '\\')
            .strip()
        )
    if not summary:
        summary = extract_json_string_field(
            text,
            ('gap', 'plain_english_summary', 'summary', 'gap_summary', 'analysis'),
        )

    if not impact_match and not summary:
        return None

    payload: dict[str, Any] = {'citations': []}
    if impact_match is not None:
        payload['impact_score'] = normalize_impact_score(impact_match.group(1))
    if summary:
        payload['gap'] = summary
        payload['plain_english_summary'] = summary
    rule = extract_json_string_field(text, ('rule', 'rules'))
    if rule:
        payload['rule'] = rule
    fix = extract_json_string_field(text, ('fix', 'remediation'))
    if fix:
        payload['fix'] = fix
    return payload


def response_json(response: Any) -> dict[str, Any]:
    return parse_response_json(
        response,
        loose_parser=parse_gap_fields_loosely,
        recovery_log_event='gap_analysis_json_recovered_with_loose_parser',
    )


async def reanalyze_stale_gaps_for_org(org_id: str) -> AgentRunResponse:
    """Re-run gap analysis for every trigger that still has fallback rows."""
    if SessionLocal is None:
        return AgentRunResponse(
            run_id=str(uuid4()),
            status='skipped',
            message='Gap analysis requires a configured database.',
        )

    token = set_tenant_org_id(org_id)
    db = SessionLocal()
    bind_data_plane_session(db, org_id=org_id)
    try:
        stale_triggers = (
            db.query(GapAnalysis.trigger_type, GapAnalysis.trigger_id)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.analysis_mode.in_(('RULE_FALLBACK', 'LLM_FAILED')),
            )
            .distinct()
            .all()
        )
    finally:
        db.close()
        reset_tenant_org_id(token)

    if not stale_triggers:
        return AgentRunResponse(
            run_id=str(uuid4()),
            status='already_processed',
            message='No fallback gap analyses need re-analysis.',
        )

    run_id = str(uuid4())
    completed = 0
    messages: list[str] = []
    for trigger_type, trigger_id in stale_triggers:
        response = await run_gap_analysis(
            AgentRunRequest(
                trigger_type=trigger_type,
                trigger_id=trigger_id,
                org_id=org_id,
                force_reanalysis=True,
                auto_generate_redlines=False,
            )
        )
        if response.status == 'completed':
            completed += 1
        messages.append(f'{trigger_type}:{trigger_id[:8]}={response.status}')

    return AgentRunResponse(
        run_id=run_id,
        status='completed' if completed else 'skipped',
        message=(
            f'Re-analyzed {completed} trigger(s) with stale fallback results. '
            + '; '.join(messages)
        ),
    )


def business_change_similarity_fields(
    metadata: dict[str, Any] | None,
) -> dict[str, float | None]:
    """Extract business-change-to-Internal-Policy cosine match metadata."""
    if not metadata:
        return {'business_change_cosine_similarity': None}
    score = metadata.get('business_change_policy_cosine_similarity')
    if score is not None:
        try:
            return {'business_change_cosine_similarity': float(score)}
        except (TypeError, ValueError):
            return {'business_change_cosine_similarity': None}
    items = metadata.get('business_change_document_similarity') or []
    if isinstance(items, list) and items and isinstance(items[0], dict):
        similarity = items[0].get('cosine_similarity')
        try:
            return {
                'business_change_cosine_similarity': (
                    float(similarity) if similarity is not None else None
                )
            }
        except (TypeError, ValueError):
            return {'business_change_cosine_similarity': None}
    return {'business_change_cosine_similarity': None}


def regulator_similarity_fields(metadata: dict[str, Any] | None) -> dict[str, float | str | None]:
    """Extract vector-comparison metadata persisted during gap analysis.

    Prefers the pooled ALL-regulators score when present; otherwise the strongest
    individual FINRA/SEC/CFTC match.
    """
    empty = {
        'regulator_cosine_similarity': None,
        'regulator_reference_policy_name': None,
    }
    if not metadata:
        return empty
    items = metadata.get('regulator_document_similarity') or []
    if not isinstance(items, list) or not items:
        return empty
    dict_items = [item for item in items if isinstance(item, dict)]
    if not dict_items:
        return empty
    overall = next(
        (
            item
            for item in dict_items
            if str(item.get('regulator_source') or '').upper() == 'ALL'
        ),
        None,
    )
    primary = overall or max(
        dict_items,
        key=lambda item: float(item.get('cosine_similarity') or 0.0),
    )
    similarity = primary.get('cosine_similarity')
    return {
        'regulator_cosine_similarity': (
            float(similarity) if similarity is not None else None
        ),
        'regulator_reference_policy_name': str(primary.get('policy_name') or '').strip() or None,
    }


def attach_regulator_similarity_to_policy_update_gap(
    db: Session,
    *,
    org_id: str,
    policy_id: str,
    similarities: Sequence[Any],
) -> None:
    """Write Internal Policy ↔ FINRA/SEC/CFTC cosine onto the Policy Update gap row."""
    payload: list[dict[str, Any]] = []
    for item in similarities:
        as_dict = getattr(item, 'as_metadata_dict', None)
        if callable(as_dict):
            payload.append(as_dict())
        elif isinstance(item, dict):
            payload.append(item)
    if not payload:
        return
    query = getattr(db, 'query', None)
    if not callable(query):
        return
    try:
        rows = list(
            query(GapAnalysis)
            .filter(GapAnalysis.org_id == org_id, GapAnalysis.policy_id == policy_id)
            .all()
        )
    except Exception:
        return
    for gap in rows:
        if not _is_policy_update_trigger(gap.trigger_type):
            continue
        metadata = dict(gap.retrieval_metadata) if isinstance(gap.retrieval_metadata, dict) else {}
        existing = metadata.get('regulator_document_similarity')
        if isinstance(existing, list) and any(
            isinstance(entry, dict) and entry.get('cosine_similarity') is not None
            for entry in existing
        ):
            continue
        metadata['regulator_document_similarity'] = payload
        gap.retrieval_metadata = metadata
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(gap, 'retrieval_metadata')
        except Exception:
            pass


def list_gap_analyses_for_org(
    db: Session | None,
    org_id: str,
    impact_score: str | None = None,
    trigger_type: str | None = None,
    limit: int = 50,
    *,
    dedupe_by_policy: bool = True,
    include_findings: bool = True,
) -> list[GapAnalysisResponse]:
    """Return Internal Policy Gaps rows for the Compliance Gaps table.

    When ``dedupe_by_policy`` is true (default), keep the newest Policy Update row
    per Internal policy name, and a separate Business Change row per change. The
    two trigger labels are never collapsed into one table row.

    When ``include_findings`` is false, omit full ``gaps`` / ``citations`` bodies so list
    pages paint faster; clients can fetch a single analysis for expand-on-demand details.
    """
    if db is None:
        return []
    query = (
        db.query(GapAnalysis, Policy, RegulatoryPublication, BusinessChange)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .outerjoin(
            RegulatoryPublication,
            RegulatoryPublication.id == GapAnalysis.publication_id,
        )
        .outerjoin(BusinessChange, BusinessChange.id == GapAnalysis.business_change_id)
        .filter(
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
    )
    if impact_score:
        normalized_filter = normalize_impact_score(impact_score)
        if normalized_filter == 'INFO':
            query = query.filter(GapAnalysis.impact_score.in_(['INFO', 'NONE']))
        else:
            query = query.filter(GapAnalysis.impact_score == normalized_filter)
    else:
        # Default table: material gaps, every Business Change, and every Policy Update
        # (including INFO after a cron refresh or upload overwrite).
        query = query.filter(
            or_(
                GapAnalysis.impact_score.in_(['HIGH', 'MEDIUM']),
                GapAnalysis.trigger_type == 'business_change',
                GapAnalysis.trigger_type.in_(['policy_update', 'publication']),
            )
        )
    if trigger_type:
        query = query.filter(GapAnalysis.trigger_type == trigger_type)

    impact_rank = case(
        (GapAnalysis.impact_score == 'HIGH', 0),
        (GapAnalysis.impact_score == 'MEDIUM', 1),
        else_=2,
    )
    # Prefer newest analysis when deduping by policy name so v8 always beats v7.
    fetch_limit = max(limit * 4, 50) if dedupe_by_policy else limit
    if dedupe_by_policy:
        records = (
            query.order_by(GapAnalysis.created_at.desc())
            .limit(fetch_limit)
            .all()
        )
    else:
        records = (
            query.order_by(impact_rank.asc(), GapAnalysis.created_at.desc())
            .limit(fetch_limit)
            .all()
        )
    cleared_gap_ids = gap_analysis_ids_cleared_by_reupload(db, org_id)
    selected: list[tuple[GapAnalysis, Policy, RegulatoryPublication | None, BusinessChange | None]] = []
    seen_gap_identities: set[str] = set()
    for analysis, policy, publication, business_change in records:
        dedupe_key = _gap_list_dedupe_key(policy.name, analysis)
        # Approved + Policy Library re-upload hides that analysis until a newer
        # Policy Update overwrite (created_at after reuploaded_at) replaces it.
        if analysis.id in cleared_gap_ids:
            continue
        if dedupe_by_policy and dedupe_key and dedupe_key in seen_gap_identities:
            continue
        metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
        findings = sentence_findings_from_metadata(metadata)
        if (
            findings
            or analysis.impact_score in {'HIGH', 'MEDIUM'}
            or _is_business_change_trigger(analysis.trigger_type)
            or _is_policy_update_trigger(analysis.trigger_type)
        ):
            if dedupe_by_policy and dedupe_key:
                seen_gap_identities.add(dedupe_key)
            selected.append((analysis, policy, publication, business_change))
        if len(selected) >= limit:
            break

    # Present HIGH before MEDIUM after newest-per-name selection.
    if dedupe_by_policy and selected:
        selected.sort(
            key=lambda row: (
                0 if row[0].impact_score == 'HIGH' else 1 if row[0].impact_score == 'MEDIUM' else 2,
                -(row[0].created_at.timestamp() if row[0].created_at else 0),
            )
        )

    gap_ids = [analysis.id for analysis, *_rest in selected]
    revisions_by_gap = load_latest_revisions_for_gaps(db, org_id, gap_ids)

    responses: list[GapAnalysisResponse] = []
    for analysis, policy, publication, business_change in selected:
        gaps = build_gap_finding_summaries(analysis) if include_findings else []
        gap_count = (
            max(len(gaps), 1 if analysis.impact_score in {'HIGH', 'MEDIUM'} else 0)
            if include_findings
            else count_gap_findings(analysis)
        )
        revision = revisions_by_gap.get(analysis.id)
        metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
        workflow_status = gap_workflow_status(revision)
        from app.services.business_change_policy_workflow_service import (
            NEW_POLICY_FROM_BUSINESS_CHANGE_KEY,
            gap_analysis_is_redline_eligible,
        )

        marketing_fields = marketing_gap_api_fields(analysis)
        policy_name = marketing_fields.pop('policy_name', None) or display_marketing_policy_name(
            policy.name
        )
        responses.append(
            GapAnalysisResponse(
                id=analysis.id,
                policy_id=policy.id,
                policy_name=policy_name,
                policy_version=policy.version,
                impact_score=normalize_impact_score(analysis.impact_score),
                trigger_type=canonical_gap_trigger_type(analysis.trigger_type),
                trigger_id=analysis.trigger_id,
                trigger_label=trigger_label_for_gap(analysis, publication, business_change, policy),
                business_change_type=(
                    business_change.change_type if business_change is not None else None
                ),
                gap_summary=analysis.gap_summary,
                rule=analysis.rule if include_findings else None,
                fix=analysis.fix if include_findings else None,
                gap_count=gap_count,
                gaps=gaps,
                citations=(
                    citation_responses(analysis.regulatory_citations) if include_findings else []
                ),
                retrieval_mode=str((analysis.retrieval_metadata or {}).get(
                    'retrieval_mode') or 'LEGACY'),
                analysis_mode=analysis.analysis_mode,
                analysis_model=analysis.analysis_model,
                **regulator_similarity_fields(analysis.retrieval_metadata or {}),
                **business_change_similarity_fields(analysis.retrieval_metadata or {}),
                created_at=analysis.created_at,
                status=workflow_status,
                revision_id=revision.id if revision is not None else None,
                redline_ready=bool(
                    revision is not None
                    and revision.redline_file_url
                ),
                redline_eligible=gap_analysis_is_redline_eligible(
                    metadata,
                    trigger_type=str(analysis.trigger_type or ''),
                ),
                new_policy_from_business_change=bool(
                    metadata.get(NEW_POLICY_FROM_BUSINESS_CHANGE_KEY)
                ),
                **needs_review_decision_response_fields(
                    metadata,
                    status=workflow_status,
                ),
                **marketing_fields,
            )
        )
    return responses


def get_gap_analysis_for_org(
    db: Session | None,
    org_id: str,
    gap_analysis_id: str,
) -> GapAnalysisResponse | None:
    """Return one gap analysis with full findings for expand-on-demand UI."""
    if db is None:
        return None
    record = (
        db.query(GapAnalysis, Policy, RegulatoryPublication, BusinessChange)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .outerjoin(
            RegulatoryPublication,
            RegulatoryPublication.id == GapAnalysis.publication_id,
        )
        .outerjoin(BusinessChange, BusinessChange.id == GapAnalysis.business_change_id)
        .filter(
            GapAnalysis.id == gap_analysis_id,
            GapAnalysis.org_id == org_id,
            Policy.org_id == org_id,
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .one_or_none()
    )
    if record is None:
        return None
    analysis, policy, publication, business_change = record
    gaps = build_gap_finding_summaries(analysis)
    revisions = load_latest_revisions_for_gaps(db, org_id, [analysis.id])
    revision = revisions.get(analysis.id)
    metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
    workflow_status = gap_workflow_status(revision)
    from app.services.business_change_policy_workflow_service import (
        NEW_POLICY_FROM_BUSINESS_CHANGE_KEY,
        gap_analysis_is_redline_eligible,
    )

    marketing_fields = marketing_gap_api_fields(analysis)
    policy_name = marketing_fields.pop('policy_name', None) or display_marketing_policy_name(
        policy.name
    )
    return GapAnalysisResponse(
        id=analysis.id,
        policy_id=policy.id,
        policy_name=policy_name,
        policy_version=policy.version,
        impact_score=normalize_impact_score(analysis.impact_score),
        trigger_type=canonical_gap_trigger_type(analysis.trigger_type),
        trigger_id=analysis.trigger_id,
        trigger_label=trigger_label_for_gap(analysis, publication, business_change, policy),
        business_change_type=(
            business_change.change_type if business_change is not None else None
        ),
        gap_summary=analysis.gap_summary,
        rule=analysis.rule,
        fix=analysis.fix,
        gap_count=max(len(gaps), 1 if analysis.impact_score in {'HIGH', 'MEDIUM'} else 0),
        gaps=gaps,
        citations=citation_responses(analysis.regulatory_citations),
        retrieval_mode=str((analysis.retrieval_metadata or {}).get('retrieval_mode') or 'LEGACY'),
        analysis_mode=analysis.analysis_mode,
        analysis_model=analysis.analysis_model,
        **regulator_similarity_fields(analysis.retrieval_metadata or {}),
        **business_change_similarity_fields(analysis.retrieval_metadata or {}),
        created_at=analysis.created_at,
        status=workflow_status,
        revision_id=revision.id if revision is not None else None,
        redline_ready=bool(revision is not None and revision.redline_file_url),
        redline_eligible=gap_analysis_is_redline_eligible(
            metadata,
            trigger_type=str(analysis.trigger_type or ''),
        ),
        new_policy_from_business_change=bool(
            metadata.get(NEW_POLICY_FROM_BUSINESS_CHANGE_KEY)
        ),
        **needs_review_decision_response_fields(
            metadata,
            status=workflow_status,
        ),
        **marketing_fields,
    )


def canonical_gap_trigger_type(value: str | None) -> str:
    trigger = str(value or '').strip()
    if trigger in KNOWN_GAP_TRIGGER_TYPES:
        return trigger
    return 'publication'


def citation_responses(value: Any) -> list[GapCitationResponse]:
    """Read both Phase 5 citation objects and safely display legacy string data."""
    if not isinstance(value, list):
        return []
    citations: list[GapCitationResponse] = []
    for item in value:
        if isinstance(item, dict):
            reference = str(item.get('reference') or '').strip()
            if reference:
                citations.append(
                    GapCitationResponse(
                        source=str(item.get('source') or 'Trigger source'),
                        reference=reference,
                        quote=str(item.get('quote') or '').strip() or None,
                        rationale=str(item.get('rationale')
                                      or '').strip() or None,
                    )
                )
        elif isinstance(item, str) and item.strip():
            citations.append(GapCitationResponse(
                source='Trigger source', reference=item.strip()))
    return citations


def delete_gap_analysis_for_org(
    db: Session,
    org_id: str,
    gap_analysis_id: str,
) -> bool:
    """Force-delete one gap analysis row for the org.

    Related ``policy_revisions`` cascade-delete via FK. Compliance history keeps the
    historical incident with ``gap_analysis_id`` set null. Audit logs are append-only
    and retain orphaned references.
    """
    analysis = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.id == gap_analysis_id,
            GapAnalysis.org_id == org_id,
        )
        .one_or_none()
    )
    if analysis is None:
        return False

    revision_count = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
        )
        .count()
    )
    db.delete(analysis)
    db.commit()
    logger.info(
        'gap_analysis_force_deleted',
        org_id=org_id,
        gap_analysis_id=gap_analysis_id,
        cascaded_revisions=revision_count,
    )
    return True
