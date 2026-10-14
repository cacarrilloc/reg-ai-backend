"""Match business changes to Internal Policies and open Gap Analysis / redline paths."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from uuid import uuid4

import structlog
from docx import Document
from sqlalchemy.orm import Session

from app.ai import ChatMessage, LLMProvider, embedding_model_label, optional_llm_provider
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal, session_scope
from app.models.core import BusinessChange, DocumentChunk, GapAnalysis, Policy
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.services.document_chunk_service import (
    load_internal_policy_vectors,
    mean_max_cosine_similarity,
)
from app.services.document_ingestion import chunk_publication_text, generate_embeddings
from app.services.gap_analysis_service import (
    GapFinding,
    persist_gap_analysis_result,
)
from app.services.llm_json import response_json
from app.services.llm_persona import (
    internal_policy_analysis_workflow,
    with_internal_policy_analysis_persona,
)
from app.services.policy_service import (
    next_policy_version,
    persist_policy_upload,
    store_policy_file,
)
from app.services.policy_types import REGULATORY_REFERENCE_TYPE
from app.services.storage_service import remove_file

logger = structlog.get_logger(__name__)

BUSINESS_CHANGE_POLICY_MATCH_MAX_TOKENS = 1024
MAX_POLICY_EXCERPT_CHARS = 4_000
NEW_POLICY_FROM_BUSINESS_CHANGE_KEY = 'new_policy_from_business_change'
BUSINESS_CHANGE_POLICY_MATCH_KEY = 'business_change_policy_match'
BUSINESS_CHANGE_COSINE_KEY = 'business_change_policy_cosine_similarity'
BUSINESS_CHANGE_DOCUMENT_SIMILARITY_KEY = 'business_change_document_similarity'
REDLINE_ELIGIBLE_KEY = 'redline_eligible'
BUSINESS_CHANGE_POLICY_COSINE_FLOOR = 0.25

BUSINESS_CHANGE_POLICY_MATCH_SYSTEM_PROMPT = with_internal_policy_analysis_persona(
    """You match an internal business change to one Internal Policy document.

After Phase 1A (deep Internal Policy understanding) and Phase 1B (deep business_change
understanding), decide whether the business-change wording describes a change that would require
updating that policy (same subject matter, controls, products, processes, staffing,
communications, or recordkeeping covered by that policy). Do not match from keywords alone.

Return only JSON (emit phase1_brief first):
{
  "phase1_brief": {
    "policy_understanding": "concise summary of what this Internal Policy covers",
    "companion_understanding": "concise summary of the business change",
    "covered_well": "overlap already addressed by the policy",
    "silent_or_thin": "whether the change falls outside or weakly intersects this policy"
  },
  "matched": boolean,
  "confidence": number,
  "rationale": string
}

confidence is 0.0–1.0. Set matched=true only when the policy is clearly the right document
to revise for this change. Be conservative: prefer false when the connection is weak."""
)


@dataclass(frozen=True)
class PolicyMatchCandidate:
    policy_id: str
    policy_name: str
    policy_version: int
    excerpt: str
    vectors: tuple[tuple[float, ...], ...] = ()


@dataclass(frozen=True)
class BusinessChangeSnapshot:
    id: str
    org_id: str
    change_type: str
    description: str
    event_summary: str
    triggers_review: bool


@dataclass(frozen=True)
class BusinessChangePolicyMatchResult:
    matched: bool
    matched_policy_ids: list[str]
    rationale: str
    mode: str
    cosine_scores: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class BusinessChangePolicyWorkflowResult:
    match: BusinessChangePolicyMatchResult
    gap_analysis: AgentRunResponse | None
    new_policy_id: str | None
    new_gap_analysis_id: str | None


def load_internal_policy_match_candidates(
    db: Session,
    org_id: str,
) -> list[PolicyMatchCandidate]:
    """Load ACTIVE Internal Policy documents for business-change matching."""
    policies = (
        db.query(Policy)
        .filter(
            Policy.org_id == org_id,
            Policy.status == 'ACTIVE',
            Policy.type != REGULATORY_REFERENCE_TYPE,
        )
        .order_by(Policy.name.asc())
        .all()
    )
    candidates: list[PolicyMatchCandidate] = []
    for policy in policies:
        chunks = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.policy_id == policy.id, DocumentChunk.org_id == org_id)
            .order_by(DocumentChunk.chunk_index.asc())
            .limit(4)
            .all()
        )
        excerpt = '\n\n'.join(chunk.content for chunk in chunks if chunk.content).strip()
        if not excerpt:
            excerpt = f'Policy name: {policy.name}. No stored body text is available.'
        vectors = load_internal_policy_vectors(
            db,
            org_id=org_id,
            policy_id=policy.id,
            policy_name=policy.name,
        )
        candidates.append(
            PolicyMatchCandidate(
                policy_id=policy.id,
                policy_name=policy.name,
                policy_version=policy.version,
                excerpt=excerpt[:MAX_POLICY_EXCERPT_CHARS],
                vectors=tuple(tuple(float(value) for value in vector) for vector in vectors),
            )
        )
    return candidates


def business_change_embedding_text(
    change: BusinessChange | BusinessChangeSnapshot,
) -> str:
    """Deep-read text used to embed a submitted business change."""
    parts = [
        f'Change type: {change.change_type}',
        change.description.strip(),
        (change.event_summary or '').strip(),
    ]
    return '\n\n'.join(part for part in parts if part)


async def embed_business_change(
    change: BusinessChange | BusinessChangeSnapshot,
    settings: Settings,
) -> list[list[float]]:
    """Create embedding vectors for the assessed business-change text."""
    text = business_change_embedding_text(change)
    if not text:
        return []
    chunks = chunk_publication_text(f'business-change-{change.id}', text)
    return await generate_embeddings(chunks, settings, org_id=change.org_id)


def score_internal_policies_by_cosine(
    change_vectors: list[list[float]],
    candidates: list[PolicyMatchCandidate],
) -> list[tuple[float, PolicyMatchCandidate]]:
    """Rank Internal Policies by mean-max cosine similarity to the business change."""
    if not change_vectors:
        return []
    scored: list[tuple[float, PolicyMatchCandidate]] = []
    for candidate in candidates:
        policy_vectors = [list(vector) for vector in candidate.vectors]
        if not policy_vectors:
            continue
        score = mean_max_cosine_similarity(change_vectors, policy_vectors)
        scored.append((score, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored


async def match_business_change_to_internal_policies(
    change: BusinessChange | BusinessChangeSnapshot,
    candidates: list[PolicyMatchCandidate],
    settings: Settings,
    client: LLMProvider | None = None,
    change_vectors: list[list[float]] | None = None,
) -> BusinessChangePolicyMatchResult:
    """Match the business-change embedding to Internal Policy vectors, then fall back."""
    if not candidates:
        return BusinessChangePolicyMatchResult(
            matched=False,
            matched_policy_ids=[],
            rationale='No active Internal Policy documents were available to match.',
            mode='NO_POLICIES',
        )

    cosine_ranked = score_internal_policies_by_cosine(change_vectors or [], candidates)
    if cosine_ranked and cosine_ranked[0][0] >= BUSINESS_CHANGE_POLICY_COSINE_FLOOR:
        best_score, best = cosine_ranked[0]
        return BusinessChangePolicyMatchResult(
            matched=True,
            matched_policy_ids=[best.policy_id],
            rationale=(
                f'Cosine match selected {best.policy_name} '
                f'({best_score:.3f} mean-max similarity).'
            ),
            mode='COSINE',
            cosine_scores=((best.policy_id, best_score),),
        )

    llm = client or optional_llm_provider(
        change.org_id, 'analysis', platform_model=settings.resolved_anthropic_gap_analysis_model)
    if llm is None:
        fallback = _fallback_policy_match(change, candidates)
        if fallback.matched:
            return fallback
        return _best_available_policy_match(
            candidates,
            cosine_ranked,
            rationale=fallback.rationale,
        )

    matched_ids: list[str] = []
    rationales: list[str] = []
    for candidate in candidates:
        payload = {
            'analysis_workflow': internal_policy_analysis_workflow(),
            'business_change': {
                'id': change.id,
                'change_type': change.change_type,
                'description': change.description[:3_000],
                'event_summary': (change.event_summary or '')[:1_500],
            },
            'internal_policy': {
                'id': candidate.policy_id,
                'name': candidate.policy_name,
                'version': candidate.policy_version,
                'excerpt': candidate.excerpt,
            },
        }
        prompt = json.dumps(payload, ensure_ascii=True)
        try:
            response = await llm.generate(
                [ChatMessage('user', prompt)],
                max_tokens=BUSINESS_CHANGE_POLICY_MATCH_MAX_TOKENS,
                system=BUSINESS_CHANGE_POLICY_MATCH_SYSTEM_PROMPT,
                cache_system=True,
                throttle='opus',
                trace_name='business_change.policy_match',
            )
            data = response_json(response)
            matched = bool(data.get('matched'))
            confidence = float(data.get('confidence') or 0.0)
            rationale = str(data.get('rationale') or '').strip()
            if matched and confidence >= 0.55:
                matched_ids.append(candidate.policy_id)
                rationales.append(
                    f'{candidate.policy_name}: {rationale or "Matched by LLM."}'
                )
            elif rationale:
                rationales.append(f'{candidate.policy_name}: not matched ({rationale})')
        except Exception as exc:
            logger.warning(
                'business_change_policy_match_failed',
                change_id=change.id,
                policy_id=candidate.policy_id,
                error_type=type(exc).__name__,
            )
            continue

    if not matched_ids:
        # Fall back to lexical match when the LLM returns no hits.
        fallback = _fallback_policy_match(change, candidates)
        if fallback.matched:
            return fallback
        best_available = _best_available_policy_match(
            candidates,
            cosine_ranked,
            rationale=(
                '; '.join(rationales[:8])
                or 'No Internal Policy document matched this business change.'
            ),
        )
        if best_available.matched:
            return best_available
        return BusinessChangePolicyMatchResult(
            matched=False,
            matched_policy_ids=[],
            rationale=(
                '; '.join(rationales[:8])
                or 'No Internal Policy document matched this business change.'
            ),
            mode='ANTHROPIC',
        )

    # Prefer a single best match for redline generation; keep all for gap rows.
    return BusinessChangePolicyMatchResult(
        matched=True,
        matched_policy_ids=matched_ids,
        rationale='; '.join(rationales[:8]),
        mode='ANTHROPIC',
    )


def _fallback_policy_match(
    change: BusinessChange | BusinessChangeSnapshot,
    candidates: list[PolicyMatchCandidate],
) -> BusinessChangePolicyMatchResult:
    """Deterministic lexical match used when Anthropic is unavailable."""
    haystack = f'{change.change_type} {change.description} {change.event_summary or ""}'.lower()
    tokens = {token for token in re.findall(r'[a-z0-9]{4,}', haystack)}
    scored: list[tuple[int, PolicyMatchCandidate]] = []
    for candidate in candidates:
        policy_tokens = {
            token for token in re.findall(r'[a-z0-9]{4,}', candidate.policy_name.lower())
        }
        excerpt_tokens = {
            token for token in re.findall(r'[a-z0-9]{4,}', candidate.excerpt.lower())
        }
        overlap = len(tokens & (policy_tokens | excerpt_tokens))
        if overlap >= 2:
            scored.append((overlap, candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return BusinessChangePolicyMatchResult(
            matched=False,
            matched_policy_ids=[],
            rationale='No Internal Policy document matched this business change (fallback).',
            mode='RULE_FALLBACK',
        )
    best = scored[0][1]
    return BusinessChangePolicyMatchResult(
        matched=True,
        matched_policy_ids=[best.policy_id],
        rationale=f'Fallback lexical match selected {best.policy_name}.',
        mode='RULE_FALLBACK',
    )


def _best_available_policy_match(
    candidates: list[PolicyMatchCandidate],
    cosine_ranked: list[tuple[float, PolicyMatchCandidate]],
    *,
    rationale: str,
) -> BusinessChangePolicyMatchResult:
    """Keep Request Review available by attaching to the closest existing policy."""
    if cosine_ranked:
        best_score, best = cosine_ranked[0]
        return BusinessChangePolicyMatchResult(
            matched=True,
            matched_policy_ids=[best.policy_id],
            rationale=(
                f'{rationale} Using closest Internal Policy {best.policy_name} '
                f'({best_score:.3f} mean-max similarity).'
            ),
            mode='BEST_AVAILABLE',
            cosine_scores=((best.policy_id, best_score),),
        )
    if candidates:
        best = candidates[0]
        return BusinessChangePolicyMatchResult(
            matched=True,
            matched_policy_ids=[best.policy_id],
            rationale=f'{rationale} Using Internal Policy {best.policy_name}.',
            mode='BEST_AVAILABLE',
        )
    return BusinessChangePolicyMatchResult(
        matched=False,
        matched_policy_ids=[],
        rationale=rationale,
        mode='NO_POLICIES',
    )


def build_new_policy_docx_from_business_change(
    change: BusinessChange | BusinessChangeSnapshot,
) -> bytes:
    """Create a reviewable Internal Policy DOCX for an unmatched business change."""
    document = Document()
    title = document.add_heading('Proposed Internal Policy', level=0)
    title.alignment = 0
    document.add_paragraph('Compliance & Legal Department')
    policy_line = document.add_paragraph()
    policy_line.add_run(
        f'POLICY: {proposed_policy_name_for_business_change(change)}'
    ).bold = True

    for label, value in [
        ('Policy Number', 'TBD'),
        ('Version', '1.0'),
        ('Change Type', change.change_type),
        ('Source', 'Business Change intake'),
        ('Business Change ID', change.id),
    ]:
        row = document.add_paragraph()
        row.add_run(f'{label}: ').bold = True
        row.add_run(str(value))

    document.add_heading('1. Purpose', level=1)
    document.add_paragraph(
        'This draft Internal Policy captures a firm business change that did not match an '
        'existing Policy Library document. Review and approve this document before it becomes '
        'the firm standard.'
    )
    document.add_heading('2. Business Change Description', level=1)
    document.add_paragraph(change.description.strip() or 'No description provided.')
    if change.event_summary:
        document.add_heading('3. Compliance Event Summary', level=1)
        document.add_paragraph(change.event_summary.strip())
    document.add_heading('4. Required Review Actions', level=1)
    document.add_paragraph(
        'CCO review is required to confirm ownership, supervision, recordkeeping, and any '
        'regulator-specific controls implicated by this change.'
    )

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def proposed_policy_name_for_business_change(
    change: BusinessChange | BusinessChangeSnapshot,
) -> str:
    base = re.sub(r'\s+', ' ', (change.change_type or 'Business Change').strip()) or 'Business Change'
    return f'{base} Policy'


def policy_text_from_business_change(
    change: BusinessChange | BusinessChangeSnapshot,
) -> str:
    parts = [
        proposed_policy_name_for_business_change(change),
        change.description.strip(),
    ]
    if change.event_summary:
        parts.append(change.event_summary.strip())
    return '\n\n'.join(part for part in parts if part)


async def create_policy_and_gap_for_unmatched_business_change(
    change: BusinessChange | BusinessChangeSnapshot,
    org_id: str,
    settings: Settings,
    *,
    match_rationale: str,
) -> tuple[str, str]:
    """Persist a new Internal Policy + Gap Analysis row when no library match exists."""
    policy_id = str(uuid4())
    policy_name = proposed_policy_name_for_business_change(change)
    docx_bytes = build_new_policy_docx_from_business_change(change)
    text = policy_text_from_business_change(change)
    chunks = chunk_publication_text(policy_name, text)
    embeddings = await generate_embeddings(chunks, settings, org_id=org_id) if chunks else []

    with session_scope(org_id=org_id) as db:
        version = next_policy_version(db, org_id, policy_name)
        stored_file = store_policy_file(
            org_id=org_id,
            policy_id=policy_id,
            filename=f'{re.sub(r"[^A-Za-z0-9._-]+", "_", policy_name)}_v{version}.docx',
            contents=docx_bytes,
            content_type=(
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            ),
        )
        try:
            persist_policy_upload(
                db=db,
                policy_id=policy_id,
                org_id=org_id,
                policy_name=policy_name,
                version=version,
                file_url=stored_file.uri,
                chunks=chunks,
                embeddings=embeddings,
                embedding_model=embedding_model_label(org_id, settings),
            )
        except Exception:
            remove_file(stored_file)
            raise

        finding = GapFinding(
            impact_score='HIGH',
            summary=(
                f'No existing Internal Policy matched this business change. '
                f'A new draft policy "{policy_name}" was created for CCO review and approval. '
                f'Match rationale: {match_rationale}'
            ),
            citations=[
                {
                    'source': 'Business Change',
                    'reference': change.id,
                    'quote': change.description[:200],
                    'rationale': 'Business change produced a brand-new Internal Policy draft.',
                }
            ],
            analysis_mode='BUSINESS_CHANGE_NEW_POLICY',
            analysis_model=None,
            rule=(
                'Firm policy library must cover material business changes that create new '
                'regulated activities or controls.'
            ),
            fix=(
                f'Review and approve the newly created Internal Policy "{policy_name}", then '
                'publish it to the Policy Library.'
            ),
            sentence_findings=(),
        )
        retrieval_metadata = {
            'retrieval_mode': 'BUSINESS_CHANGE_NO_MATCH',
            NEW_POLICY_FROM_BUSINESS_CHANGE_KEY: True,
            BUSINESS_CHANGE_POLICY_MATCH_KEY: False,
            REDLINE_ELIGIBLE_KEY: False,
            'match_rationale': match_rationale,
            'sentence_findings': [],
        }
        gap_id = persist_gap_analysis_result(
            db,
            org_id=org_id,
            publication_id=None,
            business_change_id=change.id,
            trigger_type='business_change',
            trigger_id=change.id,
            policy_id=policy_id,
            finding=finding,
            retrieval_metadata=retrieval_metadata,
            force_update=True,
        )
        db.commit()
        return policy_id, gap_id


def persist_matched_business_change_gap_fallback(
    change: BusinessChange | BusinessChangeSnapshot,
    org_id: str,
    policy_id: str,
    *,
    match_rationale: str,
    cosine_score: float | None = None,
) -> str:
    """Write a visible Internal Policy Gaps row when matched LLM analysis does not persist."""
    from app.services.gap_analysis_service import GapFinding, persist_gap_analysis_result

    retrieval_metadata: dict[str, Any] = {
        'retrieval_mode': 'BUSINESS_CHANGE_MATCH_FALLBACK',
        NEW_POLICY_FROM_BUSINESS_CHANGE_KEY: False,
        BUSINESS_CHANGE_POLICY_MATCH_KEY: True,
        REDLINE_ELIGIBLE_KEY: True,
        'match_rationale': match_rationale,
        'sentence_findings': [],
    }
    if cosine_score is not None:
        retrieval_metadata[BUSINESS_CHANGE_COSINE_KEY] = round(float(cosine_score), 6)

    finding = GapFinding(
        impact_score='HIGH',
        summary=(
            f'This business change requires CCO review of the matched Internal Policy. '
            f'{match_rationale}'
        ),
        citations=[
            {
                'source': 'Business Change',
                'reference': change.id,
                'quote': (change.description or '')[:200],
                'rationale': 'Business change matched an existing Internal Policy.',
            }
        ],
        analysis_mode='BUSINESS_CHANGE_MATCH_FALLBACK',
        analysis_model=None,
        rule=(
            'Material business changes must be reviewed against the Internal Policy '
            'that governs the affected activity.'
        ),
        fix=(
            'Review the matched Internal Policy, request a redline if the control language '
            'does not cover this change, and document the CCO decision.'
        ),
        sentence_findings=(),
    )
    with session_scope(org_id=org_id) as db:
        return persist_gap_analysis_result(
            db,
            org_id=org_id,
            publication_id=None,
            business_change_id=change.id,
            trigger_type='business_change',
            trigger_id=change.id,
            policy_id=policy_id,
            finding=finding,
            retrieval_metadata=retrieval_metadata,
            force_update=True,
        )


async def process_business_change_policy_impacts(
    change_id: str,
    org_id: str,
    *,
    settings: Settings | None = None,
    client: LLMProvider | None = None,
) -> BusinessChangePolicyWorkflowResult:
    """Match → analyze matched policies, or create a new policy Gap Analysis entry."""
    from app.services.gap_analysis_service import run_gap_analysis

    resolved = settings or get_settings()
    if SessionLocal is None:
        return BusinessChangePolicyWorkflowResult(
            match=BusinessChangePolicyMatchResult(
                matched=False,
                matched_policy_ids=[],
                rationale='Database is not configured.',
                mode='SKIPPED',
            ),
            gap_analysis=None,
            new_policy_id=None,
            new_gap_analysis_id=None,
        )

    with session_scope(commit=False, org_id=org_id) as db:
        change = (
            db.query(BusinessChange)
            .filter(BusinessChange.id == change_id, BusinessChange.org_id == org_id)
            .one_or_none()
        )
        if change is None:
            raise ValueError(f'Business change {change_id} was not found for org {org_id}')
        candidates = load_internal_policy_match_candidates(db, org_id)
        # Detach values used after the session closes.
        change_snapshot = BusinessChangeSnapshot(
            id=change.id,
            org_id=change.org_id,
            change_type=change.change_type,
            description=change.description,
            event_summary=change.event_summary or '',
            triggers_review=bool(change.triggers_review),
        )

    change_vectors = await embed_business_change(change_snapshot, resolved)
    match = await match_business_change_to_internal_policies(
        change_snapshot,
        candidates,
        resolved,
        client=client,
        change_vectors=change_vectors,
    )
    logger.info(
        'business_change_policy_match_completed',
        change_id=change_id,
        org_id=org_id,
        matched=match.matched,
        matched_count=len(match.matched_policy_ids),
        mode=match.mode,
    )

    if match.matched and match.matched_policy_ids:
        gap_response: AgentRunResponse | None = None
        try:
            gap_response = await run_gap_analysis(
                AgentRunRequest(
                    trigger_type='business_change',
                    trigger_id=change_id,
                    org_id=org_id,
                    policy_ids=match.matched_policy_ids,
                    force_reanalysis=True,
                )
            )
        except Exception as exc:
            logger.exception(
                'business_change_matched_gap_analysis_failed',
                change_id=change_id,
                org_id=org_id,
                matched_policy_ids=match.matched_policy_ids,
                error_type=type(exc).__name__,
            )

        gap_status = getattr(gap_response, 'status', None) if gap_response is not None else None
        used_fallback = gap_status != 'completed'
        if used_fallback:
            cosine_by_policy = dict(match.cosine_scores)
            fallback_gap_id = persist_matched_business_change_gap_fallback(
                change_snapshot,
                org_id,
                match.matched_policy_ids[0],
                match_rationale=match.rationale,
                cosine_score=cosine_by_policy.get(match.matched_policy_ids[0]),
            )
            gap_response = AgentRunResponse(
                run_id=fallback_gap_id,
                status='completed',
                message='Persisted a business-change Internal Policy gap after analysis did not complete.',
            )
            logger.warning(
                'business_change_matched_gap_fallback_persisted',
                change_id=change_id,
                org_id=org_id,
                policy_id=match.matched_policy_ids[0],
                gap_analysis_id=fallback_gap_id,
                prior_status=gap_status,
            )

        if gap_response is None:
            raise RuntimeError('Matched business-change gap analysis produced no response.')

        logger.info(
            'business_change_matched_gap_analysis_completed',
            change_id=change_id,
            org_id=org_id,
            status=gap_response.status,
            message=gap_response.message,
            matched_policy_ids=match.matched_policy_ids,
        )
        _mark_matched_business_change_gaps_redline_eligible(
            org_id=org_id,
            change_id=change_id,
            policy_ids=match.matched_policy_ids,
            match_rationale=match.rationale,
            cosine_scores=dict(match.cosine_scores),
        )
        return BusinessChangePolicyWorkflowResult(
            match=match,
            gap_analysis=gap_response,
            new_policy_id=None,
            new_gap_analysis_id=gap_response.run_id if used_fallback else None,
        )

    new_policy_id, new_gap_id = await create_policy_and_gap_for_unmatched_business_change(
        change_snapshot,
        org_id,
        resolved,
        match_rationale=match.rationale,
    )
    return BusinessChangePolicyWorkflowResult(
        match=match,
        gap_analysis=None,
        new_policy_id=new_policy_id,
        new_gap_analysis_id=new_gap_id,
    )


def _mark_matched_business_change_gaps_redline_eligible(
    *,
    org_id: str,
    change_id: str,
    policy_ids: list[str],
    match_rationale: str,
    cosine_scores: dict[str, float] | None = None,
) -> None:
    scores = cosine_scores or {}
    with session_scope(org_id=org_id) as db:
        gaps = (
            db.query(GapAnalysis)
            .filter(
                GapAnalysis.org_id == org_id,
                GapAnalysis.trigger_type == 'business_change',
                GapAnalysis.trigger_id == change_id,
                GapAnalysis.policy_id.in_(policy_ids),
            )
            .all()
        )
        policy_names = {
            policy.id: policy.name
            for policy in db.query(Policy).filter(Policy.id.in_(policy_ids)).all()
        }
        for gap in gaps:
            metadata = dict(gap.retrieval_metadata or {})
            metadata[BUSINESS_CHANGE_POLICY_MATCH_KEY] = True
            metadata[NEW_POLICY_FROM_BUSINESS_CHANGE_KEY] = False
            metadata[REDLINE_ELIGIBLE_KEY] = True
            metadata['match_rationale'] = match_rationale
            score = scores.get(gap.policy_id)
            if score is not None:
                metadata[BUSINESS_CHANGE_COSINE_KEY] = round(float(score), 6)
                metadata[BUSINESS_CHANGE_DOCUMENT_SIMILARITY_KEY] = [
                    {
                        'policy_id': gap.policy_id,
                        'policy_name': policy_names.get(gap.policy_id) or '',
                        'cosine_similarity': round(float(score), 6),
                    }
                ]
            gap.retrieval_metadata = metadata


def gap_analysis_is_redline_eligible(metadata: dict[str, Any] | None, *, trigger_type: str) -> bool:
    """Business-change redlines are allowed only for matched Internal Policies."""
    if trigger_type == 'marketing_content':
        return bool((metadata or {}).get(REDLINE_ELIGIBLE_KEY))
    if trigger_type != 'business_change':
        return True
    if not metadata:
        return False
    if metadata.get(NEW_POLICY_FROM_BUSINESS_CHANGE_KEY):
        return False
    if REDLINE_ELIGIBLE_KEY in metadata:
        return bool(metadata.get(REDLINE_ELIGIBLE_KEY))
    return True
