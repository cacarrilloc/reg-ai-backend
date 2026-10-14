"""Mirror homepage marketing reviews into the Regulation Gap Analysis table."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.core import GapAnalysis, MarketingContent, MarketingReview, Policy, PolicyRevision
from app.services.policy_types import MARKETING_CONTENT_TYPE

logger = structlog.get_logger(__name__)

MARKETING_GAP_TRIGGER = 'marketing_content'
MARKETING_ANALYSIS_MODE = 'MARKETING_REVIEW'
HOMEPAGE_EXCERPT_LABEL = 'Homepage excerpt'
MARKETING_EXCERPT_LABELS = {
    'social': 'Social post excerpt',
    'email': 'Email excerpt',
    'webpage': HOMEPAGE_EXCERPT_LABEL,
    'presentation': 'Presentation excerpt',
    'blog': 'Blog excerpt',
    'text': 'Marketing excerpt',
    'image': 'Marketing excerpt',
    'other': 'Marketing excerpt',
}
MARKETING_RULE_CATALOG = (
    ('206(4)-1', 'SEC: Rule 206(4)-1', ('206(4)-1', '206(4)1')),
    ('204-2', 'SEC: Rule 204-2', ('204-2',)),
    ('2210', 'FINRA: Rule 2210', ('2210', '2010')),
    ('2211-2216', 'FINRA: Rules 2211–2216', ('2211', '2212', '2213', '2214', '2215', '2216')),
)
_RULE_LABEL_SUFFIXES = (
    ', the Marketing Rule',
    ', recordkeeping',
    ', communications with the public',
)
MARKETING_FORMAT_LABELS = {
    'social': 'Social post',
    'email': 'Email',
    'webpage': 'Webpage',
    'presentation': 'Presentation',
    'blog': 'Blog / article',
    'text': 'Other text',
    'image': 'Other text',
    'other': 'Other text',
}
_SHELL_TITLE_SEP = ': '
_FORMAT_LABEL_PATTERN = '|'.join(
    re.escape(label)
    for label in sorted({*MARKETING_FORMAT_LABELS.values(), 'Marketing'}, key=len, reverse=True)
)
_SHELL_DASH_RE = re.compile(rf'^({_FORMAT_LABEL_PATTERN})\s*[—–-]\s+')
_SHELL_COLON_RE = re.compile(rf'^({_FORMAT_LABEL_PATTERN}): ')


def marketing_shell_marker(content_id: str) -> str:
    return f'marketing:{content_id}'


def _content_id_from_shell_marker(file_url: str | None) -> str:
    raw = str(file_url or '').strip()
    prefix = 'marketing:'
    if raw.startswith(prefix):
        return raw[len(prefix):].strip()
    return ''


def _disambiguated_shell_name(base: str, content_id: str) -> str:
    token = ''.join(ch for ch in content_id if ch.isalnum())[:8] or 'content'
    suffix = f' · {token}'
    return f'{base[: max(1, 180 - len(suffix))]}{suffix}'[:180]


def marketing_format_label(format_name: str | None) -> str:
    raw = str(format_name or '').strip().lower()
    return MARKETING_FORMAT_LABELS.get(raw, '')


def collect_marketing_rule_codes(
    *,
    findings: list[dict[str, Any]] | None = None,
    classification: dict[str, Any] | None = None,
    cosine: dict[str, Any] | None = None,
) -> list[str]:
    blob_parts: list[str] = []
    for item in findings or []:
        if isinstance(item, dict):
            blob_parts.append(str(item.get('rule') or ''))
    if isinstance(classification, dict):
        blob_parts.extend(str(code) for code in (classification.get('rules') or []))
    rules = (cosine or {}).get('rules') if isinstance(cosine, dict) else None
    if isinstance(rules, list):
        for item in rules:
            if not isinstance(item, dict):
                continue
            if item.get('related') is False:
                continue
            blob_parts.append(str(item.get('rule_id') or item.get('title') or ''))
    blob = ' '.join(blob_parts).lower()
    codes: list[str] = []
    for code, _label, aliases in MARKETING_RULE_CATALOG:
        if any(alias.lower() in blob for alias in aliases):
            codes.append(code)
    if not codes:
        codes = ['206(4)-1', '204-2']
    return codes


def marketing_rule_labels(codes: list[str]) -> list[str]:
    wanted = set(codes)
    return [label for code, label, _aliases in MARKETING_RULE_CATALOG if code in wanted]


_RULE_ID_RE = re.compile(
    r'(SEC|FINRA):\s*Rules?\s*[0-9().]+(?:\s*[–-]\s*[0-9]+)?',
    re.I,
)


def _short_rule_label(label: str) -> str:
    text = str(label or '').strip()
    match = _RULE_ID_RE.search(text)
    if match:
        return re.sub(r'\s+', ' ', match.group(0)).strip()
    for suffix in _RULE_LABEL_SUFFIXES:
        text = re.sub(re.escape(suffix), '', text, flags=re.I)
    return re.sub(r'[,\s]+$', '', text).strip()


def marketing_gap_api_fields(analysis: GapAnalysis) -> dict[str, Any]:
    if str(analysis.trigger_type or '') != MARKETING_GAP_TRIGGER:
        return {
            'marketing_format': None,
            'marketing_format_label': None,
            'marketing_rule_labels': [],
        }
    metadata = analysis.retrieval_metadata if isinstance(analysis.retrieval_metadata, dict) else {}
    stored_codes = metadata.get('marketing_rule_codes')
    stored = metadata.get('marketing_rule_labels')
    labels: list[str] = []
    if isinstance(stored_codes, list) and stored_codes:
        labels = marketing_rule_labels([str(code) for code in stored_codes])
    elif isinstance(stored, list) and stored:
        labels = [_short_rule_label(str(item)) for item in stored if str(item).strip()]
    if not labels:
        codes = collect_marketing_rule_codes(
            findings=list(metadata.get('sentence_findings') or []),
            classification=metadata.get('classification')
            if isinstance(metadata.get('classification'), dict)
            else None,
            cosine=metadata.get('homepage_rule_similarity')
            if isinstance(metadata.get('homepage_rule_similarity'), dict)
            else None,
        )
        if '204-2' not in codes:
            codes.append('204-2')
        labels = marketing_rule_labels(codes)
    format_name = str(metadata.get('marketing_format') or '').strip() or None
    return {
        'marketing_format': format_name,
        'marketing_format_label': marketing_format_label(format_name) or 'Webpage',
        'marketing_rule_labels': labels,
    }


def display_marketing_policy_name(name: str | None) -> str:
    """Canonical Internal Policy label: ``Presentation: Reg-AI Pitch Deck``."""
    return _SHELL_DASH_RE.sub(r'\1: ', str(name or '').strip(), count=1)


def marketing_shell_name_aliases(name: str | None) -> list[str]:
    display = display_marketing_policy_name(name)
    aliases = [display]
    legacy = _SHELL_COLON_RE.sub(r'\1 — ', display, count=1)
    if legacy and legacy not in aliases:
        aliases.append(legacy)
    raw = str(name or '').strip()
    if raw and raw not in aliases:
        aliases.append(raw)
    return aliases


def marketing_shell_policy_name(content: MarketingContent) -> str:
    format_label = marketing_format_label(content.format) or 'Marketing'
    title = str(content.title or content.name or 'Untitled content').strip()[:120]
    host = ''
    if content.source_url:
        host = (urlparse(content.source_url).hostname or '').removeprefix('www.')
    if host and host.lower() not in title.lower():
        return f'{format_label}{_SHELL_TITLE_SEP}{title} ({host})'[:180]
    return f'{format_label}{_SHELL_TITLE_SEP}{title}'[:180]


def selected_or_recommended_option(finding: dict[str, Any]) -> str:
    return str(
        finding.get('selected_option') or finding.get('recommended_option') or ''
    ).strip()


def impact_for_marketing_finding(finding: dict[str, Any]) -> str:
    option = selected_or_recommended_option(finding).lower()
    status = str(finding.get('status') or '').strip().lower()
    if option in {'pull', 'redline', 'escalate'}:
        return 'HIGH'
    if option == 'add_disclosure' or status == 'held':
        return 'MEDIUM'
    if option == 'do_nothing':
        return 'INFO'
    return 'MEDIUM'


def marketing_finding_headline(item: dict[str, Any]) -> str:
    excerpt = str(item.get('excerpt') or '').strip()
    title = str(item.get('title') or '').strip()
    if excerpt and ' ' in excerpt and not excerpt[:1].isdigit() and '%' not in excerpt[:8]:
        return excerpt[:240]
    if excerpt and title and title.lower() not in excerpt.lower():
        return f'"{excerpt}" — {title}'[:240]
    return (title or excerpt)[:240]


def _option_labels(item: dict[str, Any]) -> dict[str, Any]:
    raw = item.get('option_labels')
    return raw if isinstance(raw, dict) else {}


def format_marketing_option(item: dict[str, Any]) -> str:
    selected = selected_or_recommended_option(item)
    recommended = str(item.get('recommended_option') or '').strip()
    labels = _option_labels(item)
    label = str(labels.get(selected) or selected.replace('_', ' ').title() or '').strip()
    if not label:
        return ''
    suffix = 'selected' if selected and selected != recommended else 'recommended'
    if suffix in label.lower():
        return label
    if label.endswith(')'):
        return f'{label[:-1]}, {suffix})'
    return f'{label} ({suffix})'


def apply_selected_options(
    findings: list[dict[str, Any]],
    selected_options: list[dict[str, str]] | None,
) -> list[dict[str, Any]]:
    """Stamp CCO choices onto findings without dropping the agent's recommendation."""
    chosen = {
        str(item.get('finding_id') or '').strip(): str(item.get('option') or '').strip()
        for item in (selected_options or [])
        if str(item.get('finding_id') or '').strip() and str(item.get('option') or '').strip()
    }
    applied: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        clone = dict(item)
        finding_id = str(clone.get('id') or '').strip()
        if finding_id in chosen:
            clone['selected_option'] = chosen[finding_id]
        applied.append(clone)
    return applied


def format_confidence(value: str | None) -> str:
    raw = str(value or '').strip().replace('_', ' ')
    if not raw:
        return 'Certain'
    return raw[:1].upper() + raw[1:]


def map_marketing_findings(
    findings: list[dict[str, Any]],
    *,
    format_name: str | None = None,
) -> list[dict[str, Any]]:
    """Map CCO findings into Finding / Rule / Gap / Confidence / Options rows."""
    excerpt_label = MARKETING_EXCERPT_LABELS.get(
        str(format_name or '').strip().lower(),
        'Marketing excerpt',
    ) or 'Marketing excerpt'
    mapped: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        why = str(item.get('why') or '').strip()
        finding = marketing_finding_headline(item)
        if not why and not finding:
            continue
        option = selected_or_recommended_option(item)
        labels = _option_labels(item)
        fix = str(item.get('draft_text') or labels.get(option) or option or '').strip()
        options_display = format_marketing_option(item)
        mapped.append({
            'display_kind': 'marketing',
            'finding': finding,
            'sentence': finding,
            'sentence_label': excerpt_label,
            'rule': str(item.get('rule') or '').strip() or '206(4)-1',
            'gap': (why or finding)[:2_000],
            'why': (why or finding)[:2_000],
            'fix': fix[:2_000],
            'excerpt': str(item.get('excerpt') or '').strip(),
            'draft_text': str(item.get('draft_text') or '').strip(),
            'region': str(item.get('region') or '').strip() or None,
            'selected_option': str(item.get('selected_option') or '').strip() or None,
            'recommended_option': str(item.get('recommended_option') or '').strip() or None,
            'confidence': format_confidence(str(item.get('confidence') or '')),
            'options': [
                str(choice) for choice in (item.get('options') or []) if choice and choice != 'held'
            ],
            'options_display': options_display,
            'impact_score': impact_for_marketing_finding(item),
            'finding_id': str(item.get('id') or ''),
        })
    return mapped


def _highest_impact(scores: list[str]) -> str:
    if 'HIGH' in scores:
        return 'HIGH'
    if 'MEDIUM' in scores:
        return 'MEDIUM'
    return 'INFO'


def _citations_from_cosine(cosine: dict[str, Any] | None) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    rules = (cosine or {}).get('rules') if isinstance(cosine, dict) else None
    if not isinstance(rules, list):
        return citations
    for item in rules:
        if not isinstance(item, dict) or not item.get('related'):
            continue
        reference = str(item.get('url') or item.get('rule_id') or '').strip()
        if not reference:
            continue
        score = item.get('cosine_similarity')
        citations.append({
            'source': str(item.get('regulator') or 'SEC'),
            'reference': reference,
            'quote': str(item.get('title') or item.get('rule_id') or '')[:400],
            'rationale': f'mean-max cosine {score}' if score is not None else 'cosine-related rule',
        })
    return citations[:8]


def _stamp_marketing_shell(
    policy: Policy,
    *,
    marker: str,
    name: str,
    now: datetime,
) -> Policy:
    policy.file_url = marker
    policy.name = name
    policy.status = 'ACTIVE'
    policy.updated_at = now
    return policy


def _shell_by_marker(db: Session, *, org_id: str, marker: str) -> Policy | None:
    return (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type == MARKETING_CONTENT_TYPE,
            Policy.file_url == marker,
        )
        .one_or_none()
    )


def _shell_by_name(
    db: Session,
    *,
    org_id: str,
    name: str,
    exclude_id: str | None = None,
) -> Policy | None:
    for candidate in marketing_shell_name_aliases(name):
        query = (
            db.query(Policy)
            .filter(
                Policy.org_id == org_id,
                Policy.type == MARKETING_CONTENT_TYPE,
                Policy.name == candidate,
            )
        )
        if exclude_id:
            query = query.filter(Policy.id != exclude_id)
        match = query.order_by(Policy.version.desc()).first()
        if match is not None:
            return match
    return None


def _marketing_content_exists(db: Session, *, org_id: str, content_id: str) -> bool:
    if not content_id:
        return False
    return (
        db.query(MarketingContent)
        .filter(MarketingContent.org_id == org_id, MarketingContent.id == content_id)
        .one_or_none()
    ) is not None


def _shell_is_reusable(db: Session, policy: Policy, content: MarketingContent) -> bool:
    owner_id = _content_id_from_shell_marker(policy.file_url)
    if owner_id == content.id:
        return True
    return not _marketing_content_exists(db, org_id=content.org_id, content_id=owner_id)


def ensure_marketing_shell_policy(db: Session, content: MarketingContent) -> Policy:
    """One MARKETING_CONTENT policy row per piece; reuse leftover name collisions.

    Re-analyzing the same public URL often creates a new MarketingContent id, but
    ``policies_org_name_version_key`` still keys off the display name. Look up by
    content marker first, then adopt an orphaned shell with the same name.
    """
    marker = marketing_shell_marker(content.id)
    base_name = marketing_shell_policy_name(content)
    now = datetime.now(UTC).replace(tzinfo=None)

    existing = _shell_by_marker(db, org_id=content.org_id, marker=marker)
    if existing is not None:
        name = base_name
        if _shell_by_name(db, org_id=content.org_id, name=name, exclude_id=existing.id):
            name = _disambiguated_shell_name(base_name, content.id)
        return _stamp_marketing_shell(existing, marker=marker, name=name, now=now)

    leftover = _shell_by_name(db, org_id=content.org_id, name=base_name)
    if leftover is not None and _shell_is_reusable(db, leftover, content):
        return _stamp_marketing_shell(leftover, marker=marker, name=base_name, now=now)

    name = (
        _disambiguated_shell_name(base_name, content.id)
        if leftover is not None
        else base_name
    )
    policy = Policy(
        id=str(uuid4()),
        org_id=content.org_id,
        name=name,
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
        file_url=marker,
        created_at=now,
        updated_at=now,
    )
    try:
        begin_nested = getattr(db, 'begin_nested', None)
        if callable(begin_nested):
            with db.begin_nested():
                db.add(policy)
                db.flush()
        else:
            db.add(policy)
            db.flush()
        return policy
    except IntegrityError:
        expunge = getattr(db, 'expunge', None)
        if callable(expunge):
            expunge(policy)
        logger.warning(
            'marketing_shell_policy_unique_conflict',
            org_id=content.org_id,
            name=base_name,
            content_id=content.id,
        )
        raced = _shell_by_marker(db, org_id=content.org_id, marker=marker)
        if raced is not None:
            return _stamp_marketing_shell(
                raced, marker=marker, name=raced.name or name, now=now
            )
        raced = _shell_by_name(db, org_id=content.org_id, name=base_name)
        if raced is not None:
            return _stamp_marketing_shell(raced, marker=marker, name=base_name, now=now)
        unique_name = _disambiguated_shell_name(base_name, content.id)
        retry = Policy(
            id=str(uuid4()),
            org_id=content.org_id,
            name=unique_name,
            type=MARKETING_CONTENT_TYPE,
            version=1,
            status='ACTIVE',
            file_url=marker,
            created_at=now,
            updated_at=now,
        )
        if callable(begin_nested):
            with db.begin_nested():
                db.add(retry)
                db.flush()
        else:
            db.add(retry)
            db.flush()
        return retry


def delete_marketing_gap_rows(db: Session, *, org_id: str, content_id: str) -> None:
    marker = marketing_shell_marker(content_id)
    gaps = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.org_id == org_id,
            GapAnalysis.trigger_type == MARKETING_GAP_TRIGGER,
            GapAnalysis.trigger_id == content_id,
        )
        .all()
    )
    gap_ids = [gap.id for gap in gaps]
    if gap_ids:
        (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.org_id == org_id,
                PolicyRevision.gap_analysis_id.in_(gap_ids),
            )
            .delete(synchronize_session=False)
        )
    for gap in gaps:
        db.delete(gap)
    shells = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.type == MARKETING_CONTENT_TYPE,
            Policy.file_url == marker,
        )
        .all()
    )
    for policy in shells:
        db.delete(policy)


def _cco_queue_placeholder_finding(
    content: MarketingContent,
    review: MarketingReview,
) -> dict[str, Any]:
    """Minimal finding so Send to CCO Review still creates a Review Queue row."""
    del review
    title = str(getattr(content, 'title', None) or getattr(content, 'name', None) or 'Marketing content')
    excerpt_label = MARKETING_EXCERPT_LABELS.get(
        str(getattr(content, 'format', None) or '').strip().lower(),
        'Marketing excerpt',
    ) or 'Marketing excerpt'
    return {
        'display_kind': 'marketing',
        'finding': title,
        'sentence': title,
        'sentence_label': excerpt_label,
        'rule': '206(4)-1',
        'gap': 'Sent to CCO review.',
        'why': 'Sent to CCO review.',
        'fix': 'Review the selected marketing content.',
        'excerpt': '',
        'draft_text': '',
        'region': None,
        'selected_option': None,
        'recommended_option': None,
        'confidence': 'Certain',
        'options': [],
        'options_display': '',
        'impact_score': 'MEDIUM',
        'finding_id': 'cco-queue',
    }


def persist_marketing_gap_analysis(
    db: Session,
    *,
    content: MarketingContent,
    review: MarketingReview,
    findings: list[dict[str, Any]],
    cosine: dict[str, Any] | None = None,
    analysis_model: str | None = None,
    selected_options: list[dict[str, str]] | None = None,
    for_cco_review: bool = False,
) -> str | None:
    """One Gap Analysis row per marketing review; native RULE/GAP/FIX dropdown items."""
    applied = apply_selected_options(findings, selected_options or review.selected_options)
    sentence_findings = map_marketing_findings(applied, format_name=content.format)
    material = [item for item in sentence_findings if item['impact_score'] in {'HIGH', 'MEDIUM'}]
    keep_rows = material or (sentence_findings if for_cco_review else [])
    if not keep_rows:
        if for_cco_review:
            keep_rows = [_cco_queue_placeholder_finding(content, review)]
        else:
            delete_marketing_gap_rows(db, org_id=content.org_id, content_id=content.id)
            return None

    policy = ensure_marketing_shell_policy(db, content)
    impact = _highest_impact([item['impact_score'] for item in keep_rows])
    first = keep_rows[0]
    format_label = marketing_format_label(content.format) or 'Marketing'
    title = str(content.title or content.name or format_label.lower())
    destination = ' sent to CCO review' if for_cco_review else ''
    summary = (
        f'{format_label} review ({title}): {len(keep_rows)} finding(s){destination}. '
        f'First: {first.get("finding") or first["gap"]}'
    )[:1_500]
    cosine_meta = {
        key: value
        for key, value in (cosine or {}).items()
        if key not in {'related_excerpts', 'excerpt_text'}
    }
    classification = (
        review.classification
        if isinstance(getattr(review, 'classification', None), dict)
        else {}
    ) or (
        content.classification
        if isinstance(getattr(content, 'classification', None), dict)
        else {}
    )
    rule_codes = collect_marketing_rule_codes(
        findings=findings,
        classification=classification,
        cosine=cosine,
    )
    if '204-2' not in rule_codes:
        rule_codes.append('204-2')
    rule_labels = marketing_rule_labels(rule_codes)
    metadata = {
        'retrieval_mode': 'MARKETING_COSINE',
        'sentence_findings': keep_rows,
        'marketing_review_id': review.id,
        'marketing_content_id': content.id,
        'marketing_format': content.format,
        'marketing_rule_codes': rule_codes,
        'marketing_rule_labels': rule_labels,
        'classification': classification,
        'source_url': content.source_url,
        'homepage_rule_similarity': cosine_meta or None,
        'marketing_content_review': True,
        'selected_options': list(selected_options or review.selected_options or []),
        'layout_notes': list(
            getattr(review, 'layout_notes', None) or getattr(content, 'layout_notes', None) or []
        ),
        'redline_eligible': bool(for_cco_review),
    }
    existing = (
        db.query(GapAnalysis)
        .filter(
            GapAnalysis.org_id == content.org_id,
            GapAnalysis.trigger_type == MARKETING_GAP_TRIGGER,
            GapAnalysis.trigger_id == content.id,
            GapAnalysis.policy_id == policy.id,
        )
        .one_or_none()
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    if existing is None:
        existing = GapAnalysis(
            id=str(uuid4()),
            org_id=content.org_id,
            publication_id=None,
            business_change_id=None,
            trigger_type=MARKETING_GAP_TRIGGER,
            trigger_id=content.id,
            policy_id=policy.id,
            impact_score=impact,
            gap_summary=summary,
            rule=first['rule'],
            fix=first['fix'],
            regulatory_citations=_citations_from_cosine(cosine),
            retrieval_metadata=metadata,
            analysis_model=analysis_model,
            analysis_mode=MARKETING_ANALYSIS_MODE,
            created_at=now,
        )
        db.add(existing)
    else:
        existing.impact_score = impact
        existing.gap_summary = summary
        existing.rule = first['rule']
        existing.fix = first['fix']
        existing.regulatory_citations = _citations_from_cosine(cosine)
        existing.retrieval_metadata = metadata
        existing.analysis_model = analysis_model
        existing.analysis_mode = MARKETING_ANALYSIS_MODE
    db.flush()
    from app.services.compliance_history_service import record_history_from_gap_row

    record_history_from_gap_row(db, existing)
    logger.info(
        'marketing_gap_persisted',
        gap_id=existing.id,
        content_id=content.id,
        format=content.format,
        finding_count=len(keep_rows),
        impact_score=impact,
        for_cco_review=for_cco_review,
    )
    return existing.id


def persist_homepage_gap_analysis(
    db: Session,
    *,
    content: MarketingContent,
    review: MarketingReview,
    findings: list[dict[str, Any]],
    cosine: dict[str, Any] | None = None,
    analysis_model: str | None = None,
) -> str | None:
    """Backward-compatible alias for analysis-time marketing gap persist."""
    return persist_marketing_gap_analysis(
        db,
        content=content,
        review=review,
        findings=findings,
        cosine=cosine,
        analysis_model=analysis_model,
        for_cco_review=False,
    )
