"""Compliance history: denormalized gap-incident metadata for the CCO dashboard."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.models.core import (
    AuditLog,
    ComplianceHistory,
    GapAnalysis,
    Policy,
    RegulatoryPublication,
)
from app.schemas.compliance_history import (
    ComplianceHistoryListResponse,
    ComplianceHistoryResponse,
)

logger = structlog.get_logger(__name__)

DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 200
ALLOWED_HISTORY_WINDOWS = frozenset({7, 30, 90})
MAX_PRIOR_CCO_JUDGMENT_EXCERPTS = 2
MAX_PRIOR_CCO_JUDGMENT_CHARS = 500
_EMAIL_RE = re.compile(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', re.IGNORECASE)
_PHONE_RE = re.compile(
    r'(?<!\w)(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}(?!\w)'
)

# Trigger metadata that must never appear as an "Affected Regulation".
_NON_REGULATORY_SOURCES = frozenset(
    {
        'policy library',
        'business change',
        'internal policy',
        'policy upload',
    }
)
_NON_REGULATORY_REFERENCE_PREFIXES = (
    'policy library',
    'policy upload',
    'business change',
)

# Compact regulation labels only — name + number, no descriptive prose.
_RULE_NUMBER = (
    r'\d+[a-z0-9.\-]*'
    r'(?:\([a-z0-9]+\))*'
    r'(?:-(?:\([a-z0-9]+\)|\d+[a-z0-9.\-]*))*'
)
_COMPACT_REGULATION_PATTERN = re.compile(
    r'(?ix)'
    r'('
    r'(?:FINRA|SEC|CFTC|MSRB|NFA)\s+Regulation\s+[A-Z0-9][\w\-]*'
    r'|'
    r'(?:FINRA|SEC|CFTC|MSRB|NFA)\s+Rules?\s+'
    + _RULE_NUMBER
    + r'(?:\s*(?:and|,|/|&)\s*'
    + _RULE_NUMBER
    + r')*'
    r'|'
    r'\d{1,2}\s*CFR(?:\s+§?\d[\d.]*)*'
    r'|'
    r'Rule\s+'
    + _RULE_NUMBER
    + r'|'
    r'§\s*'
    + _RULE_NUMBER
    + r'|'
    r'(?:Bank\s+Secrecy\s+Act|USA\s*PATRIOT\s*Act|BSA)'
    r')'
)


def sanitize_cco_judgment_notes(notes: str | None, *, max_chars: int = MAX_PRIOR_CCO_JUDGMENT_CHARS) -> str:
    """Normalize CCO notes and redact obvious contact PII before UI/LLM use."""
    text = ' '.join(str(notes or '').split()).strip()
    if not text:
        return ''
    text = _EMAIL_RE.sub('[redacted-email]', text)
    text = _PHONE_RE.sub('[redacted-phone]', text)
    return text[: max(1, max_chars)]


def prior_cco_judgment_notes_by_gap(
    db: Session,
    org_id: str,
    gap_ids: list[str],
) -> dict[str, str]:
    """Latest decision notes per gap_analysis_id (same org only)."""
    cleaned_ids = [gap_id for gap_id in gap_ids if gap_id]
    if not cleaned_ids:
        return {}
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.org_id == org_id,
            AuditLog.event_type == 'REVISION_DECISION',
            AuditLog.gap_analysis_id.in_(cleaned_ids),
            AuditLog.notes.isnot(None),
        )
        .order_by(AuditLog.timestamp.desc())
        .all()
    )
    notes_by_gap: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, AuditLog) or not row.gap_analysis_id:
            continue
        if row.gap_analysis_id in notes_by_gap:
            continue
        sanitized = sanitize_cco_judgment_notes(row.notes)
        if sanitized:
            notes_by_gap[row.gap_analysis_id] = sanitized
    return notes_by_gap


def retrieve_prior_cco_judgment_excerpts(
    db: Session,
    org_id: str,
    policy_id: str,
    *,
    limit: int = MAX_PRIOR_CCO_JUDGMENT_EXCERPTS,
) -> list[dict[str, str]]:
    """Load recent same-policy CCO review notes for gap-analysis firm context.

    Privacy/scope controls:
    - Same ``org_id`` and ``policy_id`` only (never org-wide mixing).
    - Contact PII redacted; reviewer ids/emails are omitted from prompt text.
    - Bounded count/length so older judgment cannot dominate the prompt.
    """
    org_id = (org_id or '').strip()
    policy_id = (policy_id or '').strip()
    if not org_id or not policy_id:
        return []
    row_limit = max(1, min(limit, MAX_PRIOR_CCO_JUDGMENT_EXCERPTS))
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.org_id == org_id,
            AuditLog.policy_id == policy_id,
            AuditLog.event_type == 'REVISION_DECISION',
            AuditLog.notes.isnot(None),
        )
        .order_by(AuditLog.timestamp.desc())
        .limit(row_limit * 3)
        .all()
    )
    excerpts: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, AuditLog):
            continue
        sanitized = sanitize_cco_judgment_notes(row.notes)
        if not sanitized:
            continue
        decision = ''
        if isinstance(row.metadata_json, dict):
            decision = str(row.metadata_json.get('decision') or '').strip().upper()
        decided = row.timestamp.date().isoformat() if row.timestamp else 'unknown-date'
        label = decision or 'DECISION'
        excerpts.append(
            {
                'document_type': 'prior_cco_judgment',
                'title': f'Prior CCO judgment ({label}, {decided})',
                'text': (
                    'Same Internal Policy only. Use as prior examiner/CCO judgment context; '
                    f'do not treat as current firm policy text. {sanitized}'
                ),
            }
        )
        if len(excerpts) >= row_limit:
            break
    return excerpts


def gap_finding_rule_texts(
    *,
    rule: str | None,
    retrieval_metadata: dict[str, Any] | None = None,
) -> list[str]:
    """RULE texts from Regulation Gap Analysis RULE/GAP/FIX findings."""
    labels: list[str] = []
    for finding_rule in _sentence_finding_rules(retrieval_metadata):
        normalized = ' '.join(finding_rule.split()).strip()
        if normalized and normalized not in labels:
            labels.append(normalized)
    if labels:
        return labels

    top_level = ' '.join((rule or '').split()).strip()
    return [top_level] if top_level else []


def regulatory_rule_labels(
    *,
    rule: str | None,
    citations: list[Any] | None,
    retrieval_metadata: dict[str, Any] | None = None,
) -> list[str]:
    """Build compact regulation labels from RULE/GAP/FIX findings and citations."""
    labels: list[str] = []

    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        source = str(citation.get('source') or '').strip()
        reference = str(citation.get('reference') or '').strip()
        if _is_non_regulatory_citation(source, reference):
            continue
        for label in compact_regulation_labels(reference):
            _append_unique(labels, label)
        if source and reference and source.upper() in {
            'FINRA', 'SEC', 'CFTC', 'MSRB', 'NFA'
        }:
            for label in compact_regulation_labels(f'{source} {reference}'):
                _append_unique(labels, label)
        for label in compact_regulation_labels(source):
            _append_unique(labels, label)

    for finding_rule in _sentence_finding_rules(retrieval_metadata):
        for label in compact_regulation_labels(finding_rule):
            _append_unique(labels, label)

    for label in compact_regulation_labels(rule):
        _append_unique(labels, label)

    return labels


def compact_regulation_labels(value: str | None) -> list[str]:
    """Extract short regulation names/numbers from free text or stored labels."""
    normalized = ' '.join((value or '').split()).strip()
    if not normalized:
        return []

    found: list[str] = []
    segments = [
        segment.strip()
        for segment in re.split(r'[;|]', normalized)
        if segment.strip()
    ] or [normalized]

    for segment in segments:
        if _is_non_regulatory_text(segment):
            continue
        for match in _COMPACT_REGULATION_PATTERN.finditer(segment):
            item = ' '.join(match.group(1).split()).strip()
            if not item:
                continue
            for expanded in _expand_regulation_label(item):
                _append_unique(found, expanded)

    if not found:
        for match in _COMPACT_REGULATION_PATTERN.finditer(normalized):
            item = ' '.join(match.group(1).split()).strip()
            if not item:
                continue
            for expanded in _expand_regulation_label(item):
                _append_unique(found, expanded)

    return found


_MULTI_RULE_PATTERN = re.compile(
    r'(?ix)^'
    r'(?P<agency>FINRA|SEC|CFTC|MSRB|NFA)\s+Rules?\s+'
    r'(?P<body>.+)$'
)
_RULE_NUMBER_TOKEN = re.compile(
    r'(?ix)'
    r'\d+[a-z0-9.\-]*'
    r'(?:\([a-z0-9]+\))*'
    r'(?:-(?:\([a-z0-9]+\)|\d+[a-z0-9.\-]*))*'
)


def _expand_regulation_label(label: str) -> list[str]:
    """Turn 'FINRA Rules 3110/3120' or 'SEC Rules 17a-3 and 17a-4' into singular labels."""
    cleaned = label.rstrip('.,;:')
    match = _MULTI_RULE_PATTERN.match(cleaned)
    if match is None:
        return [cleaned]

    agency = match.group('agency')
    numbers = [
        number.rstrip('.,;:')
        for number in _RULE_NUMBER_TOKEN.findall(match.group('body'))
    ]
    if len(numbers) <= 1:
        # Keep original wording for a single rule ("FINRA Rule 3110").
        return [cleaned]
    return [f'{agency} Rule {number}' for number in numbers]


def sanitize_regulatory_rule_labels(values: list[Any] | None) -> list[str]:
    """Re-clean stored history labels so older noisy rows still display correctly."""
    labels: list[str] = []
    for value in values or []:
        for label in compact_regulation_labels(str(value)):
            _append_unique(labels, label)
    return labels


def _sentence_finding_rules(retrieval_metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(retrieval_metadata, dict):
        return []
    raw = retrieval_metadata.get('sentence_findings')
    if not isinstance(raw, list):
        return []
    rules: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rule = str(item.get('rule') or '').strip()
        if rule:
            rules.append(rule)
    return rules


def _is_non_regulatory_citation(source: str, reference: str) -> bool:
    if source.strip().lower() in _NON_REGULATORY_SOURCES:
        return True
    return _is_non_regulatory_text(reference) or _is_non_regulatory_text(source)


def _is_non_regulatory_text(value: str) -> bool:
    lowered = value.strip().lower()
    if not lowered:
        return False
    return any(lowered.startswith(prefix) for prefix in _NON_REGULATORY_REFERENCE_PREFIXES)


def _append_unique(labels: list[str], label: str) -> None:
    if not label:
        return
    if any(_normalize_for_compare(existing) == _normalize_for_compare(label) for existing in labels):
        return
    normalized = _normalize_for_compare(label)
    for index, existing in enumerate(labels):
        existing_key = _normalize_for_compare(existing)
        if normalized and existing_key and (
            normalized in existing_key or existing_key in normalized
        ):
            if len(normalized) > len(existing_key):
                labels[index] = label
            return
    labels.append(label)


def _normalize_for_compare(value: str) -> str:
    return ''.join(character.lower() for character in value if character.isalnum())


def upsert_compliance_history_for_gap(
    db: Session,
    *,
    org_id: str,
    gap_analysis_id: str,
    policy_id: str,
    rule: str | None,
    citations: list[Any] | None,
    retrieval_metadata: dict[str, Any] | None = None,
    incident_at: datetime | None = None,
    trigger_type: str | None = None,
) -> None:
    """Insert or refresh the history row for one gap incident (idempotent by gap id)."""
    policy_name = _policy_name(db, policy_id)
    rules = gap_finding_rule_texts(
        rule=rule,
        retrieval_metadata=retrieval_metadata,
    )
    resolved_trigger = (trigger_type or '').strip() or None
    now = datetime.utcnow()
    incident = incident_at or now

    existing = (
        db.query(ComplianceHistory)
        .filter(ComplianceHistory.gap_analysis_id == gap_analysis_id)
        .one_or_none()
    )
    if isinstance(existing, ComplianceHistory):
        existing.policy_id = policy_id
        existing.policy_name = policy_name
        existing.regulatory_rules = rules
        # Always mirror gap_analyses.trigger_type (source of truth for Trigger badges).
        existing.trigger_type = resolved_trigger
        # Keep the original incident timestamp; only metadata refreshes on reanalysis.
        return

    db.add(
        ComplianceHistory(
            id=str(uuid4()),
            org_id=org_id,
            gap_analysis_id=gap_analysis_id,
            policy_id=policy_id,
            policy_name=policy_name,
            regulatory_rules=rules,
            trigger_type=resolved_trigger,
            incident_at=incident,
            created_at=now,
        )
    )


def safe_upsert_compliance_history_for_gap(
    db: Session,
    *,
    org_id: str,
    gap_analysis_id: str,
    policy_id: str,
    rule: str | None,
    citations: list[Any] | None,
    retrieval_metadata: dict[str, Any] | None = None,
    incident_at: datetime | None = None,
    trigger_type: str | None = None,
) -> None:
    """Best-effort history write so gap persistence never fails on history errors."""
    try:
        upsert_compliance_history_for_gap(
            db,
            org_id=org_id,
            gap_analysis_id=gap_analysis_id,
            policy_id=policy_id,
            rule=rule,
            citations=citations,
            retrieval_metadata=retrieval_metadata,
            incident_at=incident_at,
            trigger_type=trigger_type,
        )
    except Exception:
        logger.exception(
            'compliance_history_upsert_failed',
            org_id=org_id,
            gap_analysis_id=gap_analysis_id,
            policy_id=policy_id,
        )


def list_compliance_history_for_org(
    db: Session,
    org_id: str,
    *,
    days: int = 30,
    limit: int = DEFAULT_HISTORY_LIMIT,
    trigger_type: str | None = None,
) -> ComplianceHistoryListResponse:
    """List one table row per gap incident (same Regulation values as pending approvals)."""
    window_days = days if days in ALLOWED_HISTORY_WINDOWS else 30
    row_limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    wanted_trigger = (trigger_type or '').strip() or None

    query = db.query(ComplianceHistory).filter(
        ComplianceHistory.org_id == org_id,
        ComplianceHistory.incident_at >= cutoff,
    )
    if wanted_trigger:
        query = query.outerjoin(
            GapAnalysis,
            GapAnalysis.id == ComplianceHistory.gap_analysis_id,
        ).filter(
            or_(
                GapAnalysis.trigger_type == wanted_trigger,
                and_(
                    or_(
                        GapAnalysis.id.is_(None),
                        GapAnalysis.trigger_type.is_(None),
                    ),
                    ComplianceHistory.trigger_type == wanted_trigger,
                ),
            )
        )
    rows = query.order_by(ComplianceHistory.incident_at.desc()).limit(row_limit).all()

    gap_ids = [
        row.gap_analysis_id
        for row in rows
        if isinstance(row, ComplianceHistory) and row.gap_analysis_id
    ]
    gaps_by_id: dict[str, GapAnalysis] = {}
    if gap_ids:
        gap_rows = (
            db.query(GapAnalysis)
            .filter(GapAnalysis.id.in_(gap_ids), GapAnalysis.org_id == org_id)
            .all()
        )
        gaps_by_id = {
            gap.id: gap
            for gap in gap_rows
            if isinstance(gap, GapAnalysis)
        }
    publications_by_id = _publications_for_gaps(db, gaps_by_id)
    notes_by_gap = prior_cco_judgment_notes_by_gap(db, org_id, gap_ids)

    items: list[ComplianceHistoryResponse] = []
    for row in rows:
        if not isinstance(row, ComplianceHistory):
            continue
        rules = _pending_regulation_values(row, gaps_by_id, publications_by_id)
        if not rules:
            continue
        gap = gaps_by_id.get(row.gap_analysis_id or '')
        # Prefer the linked gap's trigger_type so Trigger badges match every other table.
        if isinstance(gap, GapAnalysis):
            resolved_trigger = (gap.trigger_type or '').strip() or None
        else:
            resolved_trigger = (row.trigger_type or '').strip() or None
        if wanted_trigger and resolved_trigger != wanted_trigger:
            continue
        cco_notes = notes_by_gap.get(row.gap_analysis_id or '') or None
        policy_name = row.policy_name
        if resolved_trigger == 'marketing_content':
            from app.services.marketing_gap_bridge import display_marketing_policy_name

            policy_name = display_marketing_policy_name(row.policy_name)
        items.append(
            ComplianceHistoryResponse(
                id=row.id,
                gap_analysis_id=row.gap_analysis_id,
                policy_id=row.policy_id,
                policy_name=policy_name,
                regulatory_rules=rules,
                trigger_type=resolved_trigger,
                incident_at=row.incident_at,
                cco_judgment_notes=cco_notes,
            )
        )
        if len(items) >= row_limit:
            break

    return ComplianceHistoryListResponse(
        items=items,
        total=len(items),
        days=window_days,
    )


def _publications_for_gaps(
    db: Session,
    gaps_by_id: dict[str, GapAnalysis],
) -> dict[str, RegulatoryPublication]:
    publication_ids = [
        gap.publication_id
        for gap in gaps_by_id.values()
        if isinstance(gap, GapAnalysis) and gap.publication_id
    ]
    if not publication_ids:
        return {}
    rows = (
        db.query(RegulatoryPublication)
        .filter(RegulatoryPublication.id.in_(publication_ids))
        .all()
    )
    return {
        publication.id: publication
        for publication in rows
        if isinstance(publication, RegulatoryPublication)
    }


def _pending_regulation_values(
    row: ComplianceHistory,
    gaps_by_id: dict[str, GapAnalysis],
    publications_by_id: dict[str, RegulatoryPublication],
) -> list[str]:
    """Match Reviews Pending Approval Regulation: marketing labels or joined RULE text."""
    gap = gaps_by_id.get(row.gap_analysis_id or '')
    if isinstance(gap, GapAnalysis):
        from app.services.marketing_gap_bridge import marketing_gap_api_fields
        from app.services.review_service import regulatory_publication_label

        marketing_labels = [
            str(label).strip()
            for label in (marketing_gap_api_fields(gap).get('marketing_rule_labels') or [])
            if str(label).strip()
        ]
        if marketing_labels:
            return marketing_labels

        publication = (
            publications_by_id.get(gap.publication_id)
            if gap.publication_id
            else None
        )
        joined = regulatory_publication_label(gap, publication)
        if joined:
            return [joined]

    return [
        str(value).strip()
        for value in (row.regulatory_rules or [])
        if str(value).strip()
    ]


def _policy_name(db: Session, policy_id: str) -> str:
    policy = db.query(Policy).filter(Policy.id == policy_id).one_or_none()
    if isinstance(policy, Policy) and policy.name.strip():
        return policy.name.strip()
    return 'Untitled policy'


def record_history_from_gap_row(db: Session, gap: GapAnalysis) -> None:
    """Upsert history metadata from an already-persisted gap row."""
    safe_upsert_compliance_history_for_gap(
        db,
        org_id=gap.org_id,
        gap_analysis_id=gap.id,
        policy_id=gap.policy_id,
        rule=gap.rule,
        citations=gap.regulatory_citations if isinstance(gap.regulatory_citations, list) else [],
        retrieval_metadata=(
            gap.retrieval_metadata if isinstance(gap.retrieval_metadata, dict) else None
        ),
        incident_at=gap.created_at,
        trigger_type=gap.trigger_type,
    )


def backfill_missing_compliance_history(
    db: Session,
    *,
    org_id: str | None = None,
    gap_ids: list[str] | None = None,
) -> int:
    """Insert compliance_history for every gap_analyses row that is missing a history row.

    Every system-created gap (publication, business_change, policy_update) must have a
    matching compliance_history row. Safe to call repeatedly (idempotent).
    """
    if gap_ids is not None and not gap_ids:
        return 0

    query = (
        db.query(GapAnalysis)
        .outerjoin(
            ComplianceHistory,
            ComplianceHistory.gap_analysis_id == GapAnalysis.id,
        )
        .filter(ComplianceHistory.id.is_(None))
    )
    if org_id:
        query = query.filter(GapAnalysis.org_id == org_id)
    if gap_ids is not None:
        query = query.filter(GapAnalysis.id.in_(gap_ids))

    missing = query.order_by(GapAnalysis.created_at.asc()).all()
    written = 0
    for gap in missing:
        if not isinstance(gap, GapAnalysis):
            continue
        try:
            upsert_compliance_history_for_gap(
                db,
                org_id=gap.org_id,
                gap_analysis_id=gap.id,
                policy_id=gap.policy_id,
                rule=gap.rule,
                citations=(
                    gap.regulatory_citations
                    if isinstance(gap.regulatory_citations, list)
                    else []
                ),
                retrieval_metadata=(
                    gap.retrieval_metadata
                    if isinstance(gap.retrieval_metadata, dict)
                    else None
                ),
                incident_at=gap.created_at,
                trigger_type=gap.trigger_type,
            )
            written += 1
        except Exception:
            logger.exception(
                'compliance_history_backfill_row_failed',
                org_id=gap.org_id,
                gap_analysis_id=gap.id,
                policy_id=gap.policy_id,
            )
    if written:
        logger.info(
            'compliance_history_backfill_completed',
            org_id=org_id,
            written=written,
        )
    return written
