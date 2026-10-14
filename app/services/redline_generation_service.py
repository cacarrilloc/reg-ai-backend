from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai import (
    ChatMessage,
    LLMProvider,
    embedding_model_label,
    is_billing_error,
    is_non_retryable_error,
    is_rate_limit_error,
    optional_llm_provider,
    retry_delay,
    user_error_message,
)
from app.core.config import Settings, get_settings
from app.core.observability import langfuse_workflow_trace, normalize_trace_tags
from app.core.security import CurrentUser
from app.core.time import utc_now
from app.db.pool_guards import background_db_slot
from app.db.session import detach_instance, session_scope
from app.models.core import (
    DocumentChunk,
    GapAnalysis,
    Organization,
    Policy,
    PolicyRevision,
    Regulation,
    RegulatoryPublication,
)
from app.schemas.review import PolicyRevisionResponse
from app.services.document_chunk_service import (
    RedlineInternalDocumentSimilarity,
    RegulatorDocumentSimilarity,
    apply_redline_similarity_metadata,
    compare_redline_to_internal_policy,
    compare_redline_to_regulator_documents_with_stored_vectors,
    load_internal_policy_vectors,
    replace_redline_chunk_embeddings,
)
from app.services.llm_json import (
    extract_json_string_field,
    response_model,
    truncate_text,
)
from app.services.llm_persona import internal_policy_analysis_workflow
from app.services.notification_service import send_revision_notification
from app.services.policy_comparison_service import (
    active_reference_policy_for_internal_name,
    lexical_government_excerpts,
    regulator_source_from_policy_name,
)
from app.services.policy_revision_document import (
    DEFAULT_POLICY_APPROVER,
    DEFAULT_POLICY_OWNER,
    align_policy_sections_to_template,
    build_redline_generation_system_prompt,
    bump_policy_version_label,
    default_policy_header_from_context,
    policy_template_dates,
    sections_from_policy_paragraphs,
    template_sections_from_paragraphs,
)
from app.services.policy_types import (
    REGULATORY_REFERENCE_TYPE,
    is_internal_policy_type,
    resolve_internal_regulator_source,
)
from app.services.redline_document import (
    extract_policy_paragraphs,
    labeled_metadata_value,
    merge_applicable_regulation,
    normalize_space,
    policy_text_from_paragraphs,
)
from app.services.redline_vector_validation_service import (
    ALL_REGULATOR_LABEL,
    NO_RELATED_REDLINE_CONTENT_MESSAGE,
    load_all_regulator_reference_embeddings,
    validate_redline_against_regulator_documents,
)
from app.services.storage_service import (
    download_file,
    remove_file,
    storage_path,
    upload_file,
)

logger = structlog.get_logger(__name__)

_inflight_redline_revision_ids: set[str] = set()
_background_redline_tasks: set[asyncio.Task[Any]] = set()
_redline_schedule_lock = asyncio.Lock()
_redline_generation_semaphore = asyncio.Semaphore(2)

REDLINE_AUTHOR = "Reg AI"
REDLINE_BUCKET = "policy-redlines"
DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_POLICY_CONTEXT_CHARS = 32_000
MAX_CITATION_COMMENT_CHARS = 12_000
MAX_EDIT_ANCHOR_CHARS = 4_000
MAX_EDIT_REPLACEMENT_CHARS = 4_000
MAX_REPLACEMENT_CHARS = 8_000
MAX_REDLINE_EDITS = 40
MAX_REGULATORY_REFERENCE_CHARS = 12_000
REDLINE_DRAFT_MAX_TOKENS = 12_192
LLM_REDLINE_MAX_ATTEMPTS = 3
LLM_REDLINE_RETRY_DELAY_SECONDS = 2.0

META_REPLACEMENT_PHRASES = (
    "reg ai draft",
    "cco review",
    "for cco",
    "gap analysis:",
    "gap analysis between",
    "based on the gap analysis",
    "the following redline document",
    "several updates are required",
    "primary gaps identified",
    "identified as high",
    "identified as medium",
)

REDLINE_GENERATION_SYSTEM_PROMPT = build_redline_generation_system_prompt()


class PolicySubsectionRevision(BaseModel):
    number: str = Field(default="", max_length=20)
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=MAX_REPLACEMENT_CHARS)


class PolicyDocumentSectionRevision(BaseModel):
    number: str = Field(default="", max_length=20)
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=MAX_REPLACEMENT_CHARS)
    subsections: list[PolicySubsectionRevision] = Field(default_factory=list)


class PolicyDocumentHeader(BaseModel):
    company_name: str = Field(default="", max_length=500)
    company_department: str = Field(default="Compliance & Legal Department", max_length=500)
    policy_name: str = Field(default="", max_length=500)
    policy_number: str = Field(default="", max_length=100)
    version: str = Field(default="1.0", max_length=50)
    effective_date: str = Field(default="", max_length=100)
    owner: str = Field(default=DEFAULT_POLICY_OWNER, max_length=500)
    applicable_regulation: str = Field(default="", max_length=2_000)
    next_review_date: str = Field(default="", max_length=100)


class PolicyApprovalFooter(BaseModel):
    approved_by: str = Field(default="", max_length=500)
    approved_date: str = Field(default="", max_length=100)


class PolicySectionRevision(BaseModel):
    heading: str | None = Field(default=None, max_length=500)
    original_text: str = Field(default="", max_length=MAX_POLICY_CONTEXT_CHARS)
    revised_text: str = Field(..., min_length=1, max_length=MAX_REPLACEMENT_CHARS)
    change_note: str = Field(default="", max_length=1_000)


class RegulatorySourceNote(BaseModel):
    policy_name: str = Field(default="", max_length=500)
    regulator: str = Field(default="", max_length=50)
    excerpt: str = Field(default="", max_length=MAX_POLICY_CONTEXT_CHARS)


class RedlineEdit(BaseModel):
    anchor_text: str = Field(default="", max_length=MAX_EDIT_ANCHOR_CHARS)
    replacement_text: str = Field(default="", max_length=MAX_EDIT_REPLACEMENT_CHARS)
    citation_comment: str = Field(default="", max_length=MAX_CITATION_COMMENT_CHARS)


class RedlineDraft(BaseModel):
    document_title: str = Field(default="", max_length=500)
    drafting_summary: str = Field(default="", max_length=2_000)
    regulatory_sources_used: list[RegulatorySourceNote] = Field(default_factory=list)
    policy_header: PolicyDocumentHeader | None = None
    policy_sections: list[PolicyDocumentSectionRevision] = Field(default_factory=list)
    approval: PolicyApprovalFooter | None = None
    sections: list[PolicySectionRevision] = Field(default_factory=list)
    edits: list[RedlineEdit] = Field(default_factory=list, max_length=MAX_REDLINE_EDITS)
    target_excerpt: str = Field(default="", max_length=MAX_POLICY_CONTEXT_CHARS)
    replacement_text: str = Field(default="", max_length=MAX_REPLACEMENT_CHARS)
    citation_comment: str = Field(default="", max_length=MAX_CITATION_COMMENT_CHARS)

    @model_validator(mode="after")
    def normalize_sections(self) -> RedlineDraft:
        if self.sections:
            return self
        if self.edits:
            self.sections = [
                PolicySectionRevision(
                    original_text=edit.anchor_text,
                    revised_text=edit.replacement_text,
                    change_note=(edit.citation_comment or self.drafting_summary)[:1_000],
                )
                for edit in self.edits
                if edit.anchor_text.strip() and edit.replacement_text.strip()
            ]
            return self
        if self.target_excerpt.strip() and self.replacement_text.strip():
            self.sections = [
                PolicySectionRevision(
                    original_text=self.target_excerpt.strip(),
                    revised_text=self.replacement_text.strip(),
                    change_note=self.drafting_summary[:1_000],
                )
            ]
        return self


@dataclass(frozen=True)
class RedlineContext:
    gap_analysis: GapAnalysis
    policy: Policy
    policy_bytes: bytes
    policy_text: str
    regulatory_references: list[dict[str, str]]
    organization_name: str = "Financial Services Firm"
    approval_chain: dict[str, Any] | None = None


def detach_redline_context(context: RedlineContext, db: Session) -> RedlineContext:
    """Detach gap-analysis and policy rows before closing the loading session."""
    detach_instance(context.gap_analysis, db)
    detach_instance(context.policy, db)
    return context


def detach_revision(revision: PolicyRevision, db: Session) -> PolicyRevision:
    """Detach a revision row before closing the loading session."""
    return detach_instance(revision, db)


async def generate_policy_redline(
    gap_analysis_id: str,
    user: CurrentUser,
    db: Session | None,
    client: LLMProvider | None = None,
    *,
    defer_generation: bool = False,
    background_tasks: Any | None = None,
) -> PolicyRevisionResponse:
    """Create a pending revision DOCX for CCO review from one material gap analysis."""
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redline generation requires a configured database",
        )
    return await generate_policy_redline_internal(
        gap_analysis_id,
        user.org_id,
        db,
        client=client,
        defer_generation=defer_generation,
        background_tasks=background_tasks,
    )


async def generate_policy_redline_internal(
    gap_analysis_id: str,
    org_id: str,
    db: Session,
    client: LLMProvider | None = None,
    *,
    defer_generation: bool = False,
    background_tasks: Any | None = None,
) -> PolicyRevisionResponse:
    """Internal redline generation entry point for automated workflows."""
    async with langfuse_workflow_trace(
        'policy-redline',
        user_id=org_id,
        session_id=gap_analysis_id,
        tags=normalize_trace_tags('policy-redline'),
        metadata={'gap_analysis_id': gap_analysis_id, 'org_id': org_id},
        input_data={'gap_analysis_id': gap_analysis_id, 'org_id': org_id},
    ):
        return await _generate_policy_redline_internal_impl(
            gap_analysis_id,
            org_id,
            db,
            client=client,
            defer_generation=defer_generation,
            background_tasks=background_tasks,
        )


async def _generate_policy_redline_internal_impl(
    gap_analysis_id: str,
    org_id: str,
    db: Session,
    client: LLMProvider | None = None,
    *,
    defer_generation: bool = False,
    background_tasks: Any | None = None,
) -> PolicyRevisionResponse:
    context = load_redline_context(gap_analysis_id, org_id, db)
    if context.gap_analysis.trigger_type == "business_change":
        from app.services.business_change_policy_workflow_service import (
            gap_analysis_is_redline_eligible,
        )

        metadata = (
            context.gap_analysis.retrieval_metadata
            if isinstance(context.gap_analysis.retrieval_metadata, dict)
            else {}
        )
        if not gap_analysis_is_redline_eligible(
            metadata,
            trigger_type="business_change",
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This business change created a new Internal Policy draft. "
                    "Approve that document in Policy Library instead of requesting a redline."
                ),
            )
    revision, newly_requested = request_review_revision(context, db)
    if newly_requested:
        settings = get_settings()
        notify_payload = {
            "revision_id": revision.id,
            "org_id": revision.org_id,
            "gap_analysis_id": context.gap_analysis.id,
            "policy_name": context.policy.name,
            "impact_score": context.gap_analysis.impact_score,
            "publication_id": context.gap_analysis.publication_id,
            "approval_chain": dict(context.approval_chain or {}),
            "organization_name": context.organization_name,
        }
        if background_tasks is not None:
            background_tasks.add_task(
                notify_new_revision_from_payload,
                notify_payload,
                settings,
            )
        else:
            await notify_new_revision_from_payload(notify_payload, settings)
    if revision.redline_file_url:
        return policy_revision_response(revision)
    if defer_generation:
        return policy_revision_response(revision)

    return await finalize_policy_redline_for_revision(revision, context, db, client)


def request_review_revision(context: RedlineContext, db: Session) -> tuple[PolicyRevision, bool]:
    """Enqueue exactly one gap analysis for CCO review; never bulk-activate other gaps.

    Returns ``(revision, newly_requested)`` so callers can notify on first request only.
    """
    gap_analysis_id = context.gap_analysis.id
    org_id = context.policy.org_id
    existing_pending = (
        db.query(PolicyRevision)
        .filter(
            PolicyRevision.gap_analysis_id == gap_analysis_id,
            PolicyRevision.org_id == org_id,
            PolicyRevision.status == "PENDING",
        )
        .order_by(PolicyRevision.created_at.desc())
        .first()
    )
    if existing_pending is not None:
        if existing_pending.review_requested_at is None:
            requested_at = utc_now()
            try:
                updated = (
                    db.query(PolicyRevision)
                    .filter(
                        PolicyRevision.id == existing_pending.id,
                        PolicyRevision.gap_analysis_id == gap_analysis_id,
                        PolicyRevision.org_id == org_id,
                        PolicyRevision.status == "PENDING",
                        PolicyRevision.review_requested_at.is_(None),
                    )
                    .update(
                        {PolicyRevision.review_requested_at: requested_at},
                        synchronize_session=False,
                    )
                )
                if updated != 1:
                    db.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Unable to enqueue policy review for this gap",
                    )
                db.commit()
                db.refresh(existing_pending)
                existing_pending.review_requested_at = requested_at
            except HTTPException:
                raise
            except SQLAlchemyError as exc:
                db.rollback()
                logger.exception(
                    "policy_review_request_persistence_failed", error_type=type(exc).__name__
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Unable to enqueue policy review",
                ) from exc
            logger.info(
                "policy_review_requested",
                gap_analysis_id=gap_analysis_id,
                org_id=org_id,
                revision_id=existing_pending.id,
            )
            return existing_pending, True
        logger.info(
            "policy_review_requested",
            gap_analysis_id=gap_analysis_id,
            org_id=org_id,
            revision_id=existing_pending.id,
        )
        return existing_pending, False

    revision = PolicyRevision(
        id=str(uuid4()),
        gap_analysis_id=gap_analysis_id,
        policy_id=context.policy.id,
        org_id=org_id,
        redline_file_url=None,
        status="PENDING",
        review_requested_at=utc_now(),
        created_at=utc_now(),
    )
    try:
        db.add(revision)
        db.commit()
        db.refresh(revision)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            "policy_redline_revision_enqueue_failed", error_type=type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to enqueue policy review",
        ) from exc
    logger.info(
        "policy_review_requested",
        gap_analysis_id=gap_analysis_id,
        org_id=org_id,
        revision_id=revision.id,
    )
    return revision, True


def get_or_create_pending_revision(context: RedlineContext, db: Session) -> PolicyRevision:
    """Deprecated alias kept for tests; always records an explicit review request."""
    revision, _newly_requested = request_review_revision(context, db)
    return revision


async def load_policy_vectors_for_validation(
    policy_id: str,
    org_id: str,
    policy_name: str,
    internal_policy_text: str,
    settings: Settings,
    db: Session | None = None,
) -> list[list[float]]:
    """Load stored internal-policy embeddings created during upload or cron backfill."""
    del internal_policy_text, settings
    if db is not None:
        return load_internal_policy_vectors(
            db,
            org_id=org_id,
            policy_id=policy_id,
            policy_name=policy_name,
        )
    with session_scope(commit=False) as scoped_db:
        return load_internal_policy_vectors(
            scoped_db,
            org_id=org_id,
            policy_id=policy_id,
            policy_name=policy_name,
        )


def persist_revision_redline_url(
    revision_id: str,
    org_id: str,
    redline_file_url: str,
    db: Session | None = None,
    *,
    redline_internal_similarity: list[RedlineInternalDocumentSimilarity] | None = None,
    redline_regulator_similarity: list[RegulatorDocumentSimilarity] | None = None,
) -> PolicyRevision:
    """Persist a generated redline URL using a short-lived session when needed."""

    def _apply_revision_update(revision: PolicyRevision, scoped_db: Session) -> PolicyRevision:
        revision.redline_file_url = redline_file_url
        gap_analysis = (
            scoped_db.query(GapAnalysis)
            .filter(
                GapAnalysis.id == revision.gap_analysis_id,
                GapAnalysis.org_id == org_id,
            )
            .one_or_none()
        )
        if gap_analysis is not None and (
            redline_internal_similarity or redline_regulator_similarity
        ):
            gap_analysis.retrieval_metadata = apply_redline_similarity_metadata(
                gap_analysis.retrieval_metadata or {},
                internal_similarity=redline_internal_similarity,
                regulator_similarity=redline_regulator_similarity,
            )
        return revision

    if db is not None:
        revision = (
            db.query(PolicyRevision)
            .filter(
                PolicyRevision.id == revision_id,
                PolicyRevision.org_id == org_id,
            )
            .one_or_none()
        )
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
        _apply_revision_update(revision, db)
        db.commit()
        db.refresh(revision)
        return revision

    with session_scope() as scoped_db:
        revision = (
            scoped_db.query(PolicyRevision)
            .filter(
                PolicyRevision.id == revision_id,
                PolicyRevision.org_id == org_id,
            )
            .one_or_none()
        )
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
        _apply_revision_update(revision, scoped_db)
        scoped_db.flush()
        return detach_instance(revision, scoped_db)


async def finalize_policy_redline_for_revision(
    revision: PolicyRevision,
    context: RedlineContext,
    db: Session | None = None,
    client: LLMProvider | None = None,
    *,
    use_fallback_on_failure: bool = True,
) -> PolicyRevisionResponse:
    """Generate the revision DOCX and attach it to an existing pending revision."""
    if revision.redline_file_url:
        return policy_revision_response(revision)

    async with _redline_generation_semaphore:
        return await _finalize_policy_redline_for_revision_impl(
            revision,
            context,
            db=db,
            client=client,
            use_fallback_on_failure=use_fallback_on_failure,
        )


async def _persist_and_compare_redline_vectors(
    context: RedlineContext,
    *,
    validation: Any,
    settings: Settings,
    db: Session | None = None,
) -> tuple[list[RedlineInternalDocumentSimilarity], list[RegulatorDocumentSimilarity]]:
    redline_vectors = list(validation.redline_vectors or [])
    redline_chunks = list(validation.redline_chunks or [])
    if not redline_vectors or not redline_chunks:
        return [], []

    def _compare_with_session(scoped_db: Session) -> tuple[
        list[RedlineInternalDocumentSimilarity],
        list[RegulatorDocumentSimilarity],
    ]:
        replace_redline_chunk_embeddings(
            scoped_db,
            org_id=context.policy.org_id,
            policy_id=context.policy.id,
            gap_analysis_id=context.gap_analysis.id,
            policy_name=context.policy.name,
            chunks=redline_chunks,
            embeddings=redline_vectors,
            embedding_model=embedding_model_label(context.policy.org_id, settings),
        )
        internal_similarity = compare_redline_to_internal_policy(
            scoped_db,
            org_id=context.policy.org_id,
            policy_id=context.policy.id,
            policy_name=context.policy.name,
            redline_vectors=redline_vectors,
        )
        regulator_similarity = compare_redline_to_regulator_documents_with_stored_vectors(
            scoped_db,
            org_id=context.policy.org_id,
            policy_id=context.policy.id,
            policy_name=context.policy.name,
            gap_analysis_id=context.gap_analysis.id,
            redline_vectors=redline_vectors,
        )
        return internal_similarity, regulator_similarity

    if db is not None:
        return _compare_with_session(db)

    with session_scope() as scoped_db:
        return _compare_with_session(scoped_db)


async def _generate_validated_redline_draft(
    context: RedlineContext,
    *,
    log_id: str,
    client: LLMProvider | None = None,
    use_fallback_on_failure: bool = True,
    db: Session | None = None,
    max_validation_retries: int | None = None,
) -> tuple[
    RedlineDraft,
    Any,
    bytes,
    list[RedlineInternalDocumentSimilarity],
    list[RegulatorDocumentSimilarity],
]:
    """Run LLM drafting and vector validation, returning the accepted tracked redline DOCX."""
    settings = get_settings()

    async def draft_redline() -> RedlineDraft:
        try:
            return await draft_replacement_language(context, settings, client)
        except HTTPException as exc:
            if not use_fallback_on_failure:
                raise
            logger.warning(
                "policy_redline_drafting_fallback",
                log_id=log_id,
                detail=str(exc.detail),
            )
            return fallback_redline_draft(context)
        except Exception as exc:
            if not use_fallback_on_failure:
                raise
            logger.warning(
                "policy_redline_drafting_fallback",
                log_id=log_id,
                error_type=type(exc).__name__,
            )
            return fallback_redline_draft(context)

    configured_retries = (
        max_validation_retries
        if max_validation_retries is not None
        else settings.redline_vector_validation_max_retries
    )
    max_attempts = 1 + max(configured_retries, 0)
    accepted_draft: RedlineDraft | None = None
    final_validation = None
    regulator_vectors: list[list[float]] = []

    def _load_regulator_vectors(scoped_db: Session) -> list[list[float]]:
        return load_all_regulator_reference_embeddings(
            scoped_db,
            org_id=context.policy.org_id,
        )

    if db is not None:
        regulator_vectors = _load_regulator_vectors(db)
    else:
        with session_scope(commit=False) as scoped_db:
            regulator_vectors = _load_regulator_vectors(scoped_db)

    if not regulator_vectors:
        from app.services.internal_policy_workflow_service import (
            refresh_org_government_reference_embeddings,
        )

        # Do not live-scrape on the download request. A FINRA/SEC/CFTC scrape
        # here exceeds the Vercel rewrite budget and returns a bare 502.
        # Upload already scrapes; this only rebuilds stored regulation vectors.
        try:
            await refresh_org_government_reference_embeddings(
                None,
                org_id=context.policy.org_id,
                settings=settings,
            )
        except Exception as exc:
            logger.warning(
                'redline_regulator_refresh_before_validation_failed',
                log_id=log_id,
                org_id=context.policy.org_id,
                error_type=type(exc).__name__,
            )
        if db is not None:
            regulator_vectors = _load_regulator_vectors(db)
        else:
            with session_scope(commit=False) as scoped_db:
                regulator_vectors = _load_regulator_vectors(scoped_db)

    accepted_redline_bytes: bytes | None = None
    for attempt in range(1, max_attempts + 1):
        draft = await draft_redline()
        # Embed and gate on the same tracked redline DOCX that will be uploaded.
        candidate_bytes = build_tracked_redline_docx(
            context.policy_bytes,
            context.policy_text,
            enrich_redline_draft_from_findings(draft, context),
        )
        # Every redline creation embeds the draft and compares it against ALL
        # FINRA/SEC/CFTC Latest Regulations vectors. Retry when similarity < 25%.
        validation = await validate_redline_against_regulator_documents(
            db,
            org_id=context.policy.org_id,
            policy_name=context.policy.name,
            redline_bytes=candidate_bytes,
            fallback_text=context.policy_text,
            settings=settings,
            regulator_vectors=regulator_vectors,
        )
        if validation.skipped or validation.accepted:
            accepted_draft = draft
            accepted_redline_bytes = candidate_bytes
            final_validation = validation
            break

        # Embedding/infra failures won't improve by regenerating the redline.
        if validation.reason in {
            "redline_embedding_failed",
            "openai_api_key_missing",
            "empty_redline_text",
        }:
            logger.warning(
                "redline_vector_validation_non_retryable",
                log_id=log_id,
                policy_id=context.policy.id,
                org_id=context.policy.org_id,
                attempt=attempt,
                reason=validation.reason,
            )
            if validation.reason == "empty_redline_text":
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=NO_RELATED_REDLINE_CONTENT_MESSAGE,
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Unable to validate the redline against regulator embeddings. "
                    "Check OPENAI_API_KEY and OpenAI billing credits."
                ),
            )

        logger.warning(
            "redline_vector_validation_retry",
            log_id=log_id,
            policy_id=context.policy.id,
            org_id=context.policy.org_id,
            attempt=attempt,
            max_attempts=max_attempts,
            similarity=round(validation.similarity, 6),
            threshold=validation.threshold,
            reason=validation.reason,
            reference_label=ALL_REGULATOR_LABEL,
        )
    else:
        logger.warning(
            "redline_vector_validation_exhausted",
            log_id=log_id,
            policy_id=context.policy.id,
            org_id=context.policy.org_id,
            max_attempts=max_attempts,
            reference_label=ALL_REGULATOR_LABEL,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=NO_RELATED_REDLINE_CONTENT_MESSAGE,
        )

    assert (
        accepted_draft is not None
        and final_validation is not None
        and accepted_redline_bytes is not None
    )
    try:
        internal_similarity, regulator_similarity = await _persist_and_compare_redline_vectors(
            context,
            validation=final_validation,
            settings=settings,
            db=db,
        )
    except Exception as exc:
        logger.warning(
            "redline_vector_persistence_skipped",
            log_id=log_id,
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        internal_similarity, regulator_similarity = [], []
    if not regulator_similarity and not final_validation.skipped:
        regulator_similarity = [
            RegulatorDocumentSimilarity(
                regulator_source='ALL',
                policy_id='all-regulators',
                policy_name=ALL_REGULATOR_LABEL,
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=final_validation.similarity,
                internal_chunk_count=len(final_validation.redline_vectors or []),
                reference_chunk_count=len(regulator_vectors),
            )
        ]
    if not internal_similarity and not final_validation.skipped:
        internal_similarity = [
            RedlineInternalDocumentSimilarity(
                policy_id=context.policy.id,
                policy_name=context.policy.name,
                document_type='uploaded',
                cosine_similarity=0.0,
                internal_chunk_count=0,
                redline_chunk_count=len(final_validation.redline_vectors or []),
            )
        ]
    return (
        accepted_draft,
        final_validation,
        accepted_redline_bytes,
        internal_similarity,
        regulator_similarity,
    )


async def _finalize_policy_redline_for_revision_impl(
    revision: PolicyRevision,
    context: RedlineContext,
    db: Session | None = None,
    client: LLMProvider | None = None,
    *,
    use_fallback_on_failure: bool = True,
) -> PolicyRevisionResponse:
    """Generate the revision DOCX and attach it to an existing pending revision."""
    if revision.redline_file_url:
        return policy_revision_response(revision)

    _accepted_draft, _final_validation, redline_bytes, internal_similarity, regulator_similarity = (
        await _generate_validated_redline_draft(
            context,
            log_id=revision.id,
            client=client,
            use_fallback_on_failure=use_fallback_on_failure,
            db=db,
        )
    )
    stored_file = upload_file(
        REDLINE_BUCKET,
        storage_path(
            context.policy.org_id,
            "policy-revisions",
            revision.id,
            redline_filename(context.policy),
        ),
        redline_bytes,
        DOCX_CONTENT_TYPE,
    )
    try:
        revision = persist_revision_redline_url(
            revision.id,
            context.policy.org_id,
            stored_file.uri,
            db,
            redline_internal_similarity=internal_similarity,
            redline_regulator_similarity=regulator_similarity,
        )
    except SQLAlchemyError as exc:
        if db is not None:
            db.rollback()
        remove_file(stored_file)
        logger.exception(
            "policy_redline_revision_persistence_failed", error_type=type(exc).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to persist generated policy redline",
        ) from exc

    return policy_revision_response(revision)


async def finalize_policy_redline_background(
    revision_id: str,
    gap_analysis_id: str,
    org_id: str,
) -> None:
    """Background worker that drafts and stores a revision DOCX for a queued review."""
    async with _redline_schedule_lock:
        if revision_id in _inflight_redline_revision_ids:
            logger.info("policy_redline_background_skipped_inflight", revision_id=revision_id)
            return
        _inflight_redline_revision_ids.add(revision_id)

    try:
        async with background_db_slot():
            with session_scope(commit=False) as db:
                revision = (
                    db.query(PolicyRevision)
                    .filter(
                        PolicyRevision.id == revision_id,
                        PolicyRevision.org_id == org_id,
                        PolicyRevision.gap_analysis_id == gap_analysis_id,
                        PolicyRevision.status == "PENDING",
                    )
                    .one_or_none()
                )
                if revision is None or revision.redline_file_url:
                    return
                if revision.review_requested_at is None:
                    logger.info(
                        "policy_redline_background_skipped_unrequested",
                        revision_id=revision_id,
                        gap_analysis_id=gap_analysis_id,
                    )
                    return
                context = load_redline_context(gap_analysis_id, org_id, db)
                revision = detach_revision(revision, db)
                context = detach_redline_context(context, db)
            await finalize_policy_redline_for_revision(revision, context, db=None)
    except HTTPException as exc:
        logger.warning(
            "policy_redline_background_skipped",
            revision_id=revision_id,
            gap_analysis_id=gap_analysis_id,
            detail=str(exc.detail),
        )
    except Exception as exc:
        logger.exception(
            "policy_redline_background_failed",
            revision_id=revision_id,
            gap_analysis_id=gap_analysis_id,
            error_type=type(exc).__name__,
        )
    finally:
        async with _redline_schedule_lock:
            _inflight_redline_revision_ids.discard(revision_id)


def gap_analysis_eligible_for_redline(
    *,
    trigger_type: str,
    impact_score: str,
    retrieval_metadata: dict[str, object] | None = None,
) -> bool:
    """Material policy gaps get background redlines; matched business-change gaps included."""
    from app.services.business_change_policy_workflow_service import (
        gap_analysis_is_redline_eligible,
    )

    if not gap_analysis_is_redline_eligible(
        retrieval_metadata if isinstance(retrieval_metadata, dict) else None,
        trigger_type=trigger_type,
    ):
        return False
    normalized = impact_score.strip().upper()
    return normalized in {"HIGH", "MEDIUM", "CRITICAL"}


async def _generate_redline_for_gap_analysis(gap_analysis_id: str, org_id: str) -> None:
    """Auto redline generation is disabled; reviews require an explicit user request."""
    logger.info(
        "gap_analysis_redline_auto_enqueue_skipped",
        gap_analysis_id=gap_analysis_id,
        org_id=org_id,
    )


def _log_background_redline_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.exception(
            "gap_analysis_redline_background_task_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )


def enqueue_gap_analysis_redline(gap_analysis_id: str, org_id: str) -> None:
    """Queue redline generation for one gap analysis (async task or daemon thread)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(
            target=lambda: asyncio.run(
                _generate_redline_for_gap_analysis(gap_analysis_id, org_id)
            ),
            daemon=True,
            name=f"gap-redline-{gap_analysis_id[:8]}",
        ).start()
        return

    task = loop.create_task(
        _generate_redline_for_gap_analysis(gap_analysis_id, org_id),
        name=f"gap-redline-{gap_analysis_id[:8]}",
    )
    _background_redline_tasks.add(task)
    task.add_done_callback(_background_redline_tasks.discard)
    task.add_done_callback(_log_background_redline_result)


def schedule_missing_gap_analysis_redlines(
    background_tasks: Any,
    org_id: str,
    gap_analyses: list[Any],
) -> None:
    """No-op: gap analyses no longer auto-enqueue review revisions."""
    del background_tasks, org_id, gap_analyses


def schedule_pending_redline_generations(
    background_tasks: Any,
    org_id: str,
    pending_revisions: list[dict[str, str | None]],
) -> None:
    """Queue background DOCX generation for pending revisions missing a redline file."""
    add_task = getattr(background_tasks, "add_task", None)
    if add_task is None:
        return

    for item in pending_revisions:
        if item.get("redline_url"):
            continue
        revision_id = str(item.get("revision_id") or "")
        gap_analysis_id = str(item.get("gap_analysis_id") or "")
        if not revision_id or not gap_analysis_id:
            continue
        if revision_id in _inflight_redline_revision_ids:
            continue
        add_task(
            finalize_policy_redline_background,
            revision_id,
            gap_analysis_id,
            org_id,
        )


async def ensure_pending_revision_redline(
    revision_id: str,
    org_id: str,
    db: Session | None = None,
) -> PolicyRevisionResponse:
    """Generate or return the redline DOCX for one pending revision."""
    with session_scope(commit=False) as scoped_db:
        revision = (
            scoped_db.query(PolicyRevision)
            .filter(
                PolicyRevision.id == revision_id,
                PolicyRevision.org_id == org_id,
                PolicyRevision.status == "PENDING",
            )
            .one_or_none()
        )
        if revision is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Revision not found")
        if revision.redline_file_url:
            try:
                download_file(revision.redline_file_url)
                return policy_revision_response(revision)
            except HTTPException:
                revision.redline_file_url = None
                scoped_db.commit()
        context = load_redline_context(revision.gap_analysis_id, org_id, scoped_db)
        revision = detach_revision(revision, scoped_db)
        context = detach_redline_context(context, scoped_db)
    return await finalize_policy_redline_for_revision(revision, context, db=None)


def load_redline_context(gap_analysis_id: str, org_id: str, db: Session) -> RedlineContext:
    record = (
        db.query(GapAnalysis, Policy)
        .join(Policy, Policy.id == GapAnalysis.policy_id)
        .filter(GapAnalysis.id == gap_analysis_id, GapAnalysis.org_id == org_id, Policy.org_id == org_id)
        .one_or_none()
    )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gap analysis not found")
    gap_analysis, policy = record
    if not is_internal_policy_type(policy.type):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Redlines apply to internal enterprise policies only",
        )
    if gap_analysis.impact_score in {"NONE", "INFO"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No redline is generated for INFO impact gap analyses",
        )
    if not policy.file_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source policy file is unavailable")

    policy_bytes = download_file(policy.file_url)
    if not is_docx_bytes(policy_bytes):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Redline generation requires a DOCX source policy",
        )
    policy_text = source_policy_text(policy_bytes)
    regulatory_references = load_regulatory_reference_sections_for_redline(
        db,
        org_id,
        policy,
        gap_analysis,
        policy_text,
    )
    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == org_id)
        .one_or_none()
    )
    organization_name = getattr(organization, "name", None) or org_id
    approval_chain = getattr(organization, "approval_chain", None) if organization is not None else None
    return RedlineContext(
        gap_analysis=gap_analysis,
        policy=policy,
        policy_bytes=policy_bytes,
        policy_text=policy_text,
        regulatory_references=regulatory_references,
        organization_name=organization_name,
        approval_chain=approval_chain if isinstance(approval_chain, dict) else None,
    )


async def notify_new_revision_from_payload(
    payload: dict[str, Any],
    settings: Settings,
) -> None:
    """Background-safe Request Review email fanout (no request DB session)."""
    from app.services.organization_service import organization_notification_recipients
    from app.services.review_service import regulatory_publication_label

    organization = Organization(
        id=str(payload.get("org_id") or ""),
        auth_org_id=str(payload.get("org_id") or ""),
        name=str(payload.get("organization_name") or payload.get("org_id") or ""),
        approval_chain=payload.get("approval_chain")
        if isinstance(payload.get("approval_chain"), dict)
        else {},
    )
    recipients = organization_notification_recipients(
        organization.auth_org_id,
        organization,
    )
    if not recipients:
        logger.info(
            "revision_notification_skipped_no_org_email",
            revision_id=payload.get("revision_id"),
            org_id=payload.get("org_id"),
        )
        return

    regulation = "—"
    gap_analysis_id = payload.get("gap_analysis_id")
    publication_id = payload.get("publication_id")
    try:
        with session_scope(commit=False) as db:
            gap_analysis = None
            publication = None
            if gap_analysis_id:
                gap_analysis = (
                    db.query(GapAnalysis)
                    .filter(GapAnalysis.id == gap_analysis_id)
                    .one_or_none()
                )
            if publication_id:
                publication = (
                    db.query(RegulatoryPublication)
                    .filter(RegulatoryPublication.id == publication_id)
                    .one_or_none()
                )
            if gap_analysis is not None:
                regulation = (
                    regulatory_publication_label(gap_analysis, publication) or "—"
                )
    except Exception as exc:
        logger.warning(
            "revision_notification_regulation_lookup_failed",
            error_type=type(exc).__name__,
        )

    dashboard_url = (
        f"{settings.frontend_url.rstrip('/')}/reviews?revision_id={payload.get('revision_id')}"
    )
    for recipient in recipients:
        try:
            await send_revision_notification(
                to_email=recipient.email,
                recipient_name=recipient.full_name,
                regulation=regulation,
                impact_score=str(payload.get("impact_score") or ""),
                policy_name=str(payload.get("policy_name") or ""),
                dashboard_url=dashboard_url,
            )
        except Exception as exc:
            logger.warning(
                "revision_notification_failed",
                error_type=type(exc).__name__,
                to_email=recipient.email,
            )


async def notify_new_revision(
    revision: PolicyRevision,
    context: RedlineContext,
    settings: Settings,
    db: Session | None = None,
) -> None:
    """Compatibility wrapper used by older call sites/tests."""
    del db
    await notify_new_revision_from_payload(
        {
            "revision_id": revision.id,
            "org_id": revision.org_id,
            "gap_analysis_id": context.gap_analysis.id,
            "policy_name": context.policy.name,
            "impact_score": context.gap_analysis.impact_score,
            "publication_id": context.gap_analysis.publication_id,
            "approval_chain": dict(context.approval_chain or {}),
            "organization_name": context.organization_name,
        },
        settings,
    )



def policy_paragraphs_for_prompt(
    policy_bytes: bytes,
    max_chars: int = MAX_POLICY_CONTEXT_CHARS,
) -> list[dict[str, Any]]:
    from app.services.tracked_redline_document import filter_policy_paragraphs_to_body

    # Omit cover-page metadata (Version, Next Review Date, etc.). The LLM must start at the
    # first numbered section/subtitle; metadata is updated deterministically after drafting.
    paragraphs = filter_policy_paragraphs_to_body(extract_policy_paragraphs(policy_bytes))
    payload: list[dict[str, Any]] = []
    used = 0
    for item in paragraphs:
        entry: dict[str, Any] = {"index": item.index, "text": item.text}
        if item.heading:
            entry["heading"] = item.heading
        size = len(item.text) + len(item.heading or "")
        if used + size > max_chars and payload:
            break
        payload.append(entry)
        used += size
    return payload


async def draft_replacement_language(
    context: RedlineContext,
    settings: Settings,
    client: LLMProvider | None = None,
) -> RedlineDraft:
    if client is None:
        client = optional_llm_provider(
            context.policy.org_id, 'redline', platform_model=settings.resolved_anthropic_redline_model)
    if client is None:
        return fallback_redline_draft(context)

    prompt = json.dumps(
        {
            "task": "sentence_level_internal_policy_redline_edits",
            "analysis_workflow": internal_policy_analysis_workflow(),
            "organization_context": {
                "company_name": context.organization_name,
                "company_department": "Compliance & Legal Department",
            },
            "internal_policy": {
                "id": context.policy.id,
                "name": context.policy.name,
                "version": context.policy.version,
                "type": "Internal",
                "regulator_scope": resolve_internal_regulator_source(context.policy.name),
                "paragraphs": policy_paragraphs_for_prompt(context.policy_bytes),
            },
            "matched_regulatory_reference_material": context.regulatory_references,
            "gap_analysis_context": {
                "impact_score": context.gap_analysis.impact_score,
                "summary": context.gap_analysis.gap_summary,
                "rule": getattr(context.gap_analysis, "rule", None),
                "fix": getattr(context.gap_analysis, "fix", None),
                "citations": context.gap_analysis.regulatory_citations,
                "sentence_findings": sentence_findings_for_redline_prompt(context.gap_analysis),
                "government_reference_excerpts": government_excerpts_from_gap(context.gap_analysis),
            },
            "output_format": "sentence_edits_with_unique_rule_gap_fix_comments",
        },
        ensure_ascii=True,
    )
    try:
        last_error: Exception | None = None
        for attempt in range(1, LLM_REDLINE_MAX_ATTEMPTS + 1):
            try:
                response = await client.generate(
                    [ChatMessage("user", prompt)],
                    max_tokens=REDLINE_DRAFT_MAX_TOKENS,
                    system=REDLINE_GENERATION_SYSTEM_PROMPT,
                    temperature=0,
                    cache_system=True,
                    throttle="sonnet",
                    trace_name="policy_redline.draft",
                    trace_metadata={"gap_analysis_id": context.gap_analysis.id},
                )
                draft = response_model(
                    response,
                    RedlineDraft,
                    string_field_limits={
                        "target_excerpt": MAX_POLICY_CONTEXT_CHARS,
                        "replacement_text": MAX_REPLACEMENT_CHARS,
                        "drafting_summary": 2_000,
                        "citation_comment": MAX_CITATION_COMMENT_CHARS,
                    },
                    loose_parser=parse_redline_fields_loosely,
                    recovery_log_event="policy_redline_json_recovered_with_loose_parser",
                )
                return validate_redline_draft(draft, context)
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "policy_redline_drafting_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:500],
                    attempt=attempt,
                    policy_id=context.policy.id,
                    rate_limited=is_rate_limit_error(exc),
                    billing_blocked=is_billing_error(exc),
                )
                if is_non_retryable_error(exc):
                    break
                if attempt < LLM_REDLINE_MAX_ATTEMPTS:
                    delay = retry_delay(exc, attempt, settings)
                    if delay > 0:
                        await asyncio.sleep(delay)
        if settings.is_production and is_billing_error(last_error):
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=user_error_message(last_error),
            ) from last_error
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="AI redline drafting failed",
            ) from last_error
        return fallback_redline_draft(context)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(
            "policy_redline_drafting_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:500],
        )
        if settings.is_production:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="AI redline drafting failed",
            ) from exc
        return fallback_redline_draft(context)


def validate_redline_draft(draft: RedlineDraft, context: RedlineContext) -> RedlineDraft:
    enriched = enrich_redline_draft_from_findings(draft, context)
    sentence_edits = dedupe_redline_edits(enriched.edits)
    if sentence_edits:
        return RedlineDraft(
            document_title=context.policy.name,
            drafting_summary=enriched.drafting_summary
            or f"Prepared {len(sentence_edits)} sentence-level redline edit(s) for CCO review.",
            regulatory_sources_used=enriched.regulatory_sources_used or [],
            edits=sentence_edits,
            citation_comment="",
        )

    template_sections = normalize_policy_sections(enriched.policy_sections)
    if not template_sections:
        template_sections = legacy_sections_to_template(enriched.sections)

    if template_sections:
        aligned_sections = [
            PolicyDocumentSectionRevision.model_validate(section)
            for section in align_policy_sections_to_template(
                [section.model_dump() for section in template_sections]
            )
        ]
        template_sections = normalize_policy_sections(aligned_sections, keep_empty_sections=True)
        header = normalize_policy_header(enriched.policy_header, context)
        approval = normalize_policy_approval(enriched.approval, header)
        return RedlineDraft(
            document_title=header.policy_name or context.policy.name,
            drafting_summary=enriched.drafting_summary or "Revision draft prepared for CCO review.",
            regulatory_sources_used=enriched.regulatory_sources_used or [],
            policy_header=header,
            policy_sections=template_sections,
            approval=approval,
            citation_comment="",
        )

    logger.warning("policy_revision_no_valid_sections", policy_id=context.policy.id)
    return fallback_redline_draft(context)


def enrich_redline_draft_from_findings(draft: RedlineDraft, context: RedlineContext) -> RedlineDraft:
    """Ensure each sentence finding becomes a unique RULE/GAP/FIX-commented edit."""
    from app.services.gap_analysis_service import (
        format_gap_rule_fix_comment,
        normalize_rule_gap_fix_comment,
    )
    from app.services.tracked_redline_document import is_cover_or_metadata_line

    findings = sentence_findings_for_redline_prompt(context.gap_analysis)
    policy_text = context.policy_text or ""
    policy_lower = policy_text.lower()
    llm_edits = list(draft.edits or [])
    used_llm: set[int] = set()
    built: list[RedlineEdit] = []

    def locate_sentence(sentence: str) -> str | None:
        cleaned = " ".join((sentence or "").split()).strip()
        if not cleaned:
            return None
        if cleaned in policy_text:
            return cleaned
        lowered = cleaned.lower()
        if lowered in policy_lower:
            start = policy_lower.index(lowered)
            return policy_text[start : start + len(cleaned)]
        # Soft match: first 48 chars of the finding sentence appear in the policy.
        prefix = lowered[:48]
        if len(prefix) >= 24 and prefix in policy_lower:
            start = policy_lower.index(prefix)
            # Prefer ending at sentence boundary when possible.
            end = start + len(cleaned)
            snippet = policy_text[start:min(end + 80, len(policy_text))]
            for stopper in (". ", ".\n", "."):
                stop_at = snippet.find(stopper)
                if stop_at > 20:
                    return snippet[: stop_at + 1].strip()
            return policy_text[start : start + len(cleaned)]
        return cleaned if cleaned else None

    def match_llm_edit(sentence: str) -> RedlineEdit | None:
        needle = " ".join((sentence or "").lower().split())
        if not needle:
            return None
        for index, edit in enumerate(llm_edits):
            if index in used_llm:
                continue
            anchor = " ".join((edit.anchor_text or "").lower().split())
            if not anchor:
                continue
            if needle in anchor or anchor in needle or needle[:40] in anchor or anchor[:40] in needle:
                used_llm.add(index)
                return edit
        return None

    body_anchor_fallback = ""
    if not any(" ".join((item.get("sentence") or "").split()).strip() for item in findings):
        from app.services.tracked_redline_document import filter_policy_paragraphs_to_body

        for paragraph in filter_policy_paragraphs_to_body(
            extract_policy_paragraphs(context.policy_bytes)
        ):
            candidate = " ".join((paragraph.text or "").split()).strip()
            if candidate and not is_cover_or_metadata_line(candidate) and len(candidate) >= 12:
                body_anchor_fallback = candidate
                break

    for finding in findings:
        sentence = " ".join((finding.get("sentence") or "").split()).strip()
        if not sentence:
            sentence = body_anchor_fallback
        if is_cover_or_metadata_line(sentence):
            continue
        rule = (finding.get("rule") or "").strip()
        gap = (finding.get("gap") or "").strip()
        fix = (finding.get("fix") or "").strip()

        matched = match_llm_edit(sentence)
        if matched is None:
            for index, edit in enumerate(llm_edits):
                if index in used_llm:
                    continue
                used_llm.add(index)
                matched = edit
                break

        if rule and gap and fix:
            comment = format_gap_rule_fix_comment(rule=rule, gap=gap, fix=fix)
        elif matched and (matched.citation_comment or "").strip():
            comment = normalize_rule_gap_fix_comment(matched.citation_comment)
        else:
            comment = format_gap_rule_fix_comment(rule=rule, gap=gap, fix=fix)
        if not comment:
            continue

        anchor = locate_sentence(sentence) or " ".join(((matched.anchor_text if matched else "") or "").split()).strip()
        replacement = " ".join(((matched.replacement_text if matched else "") or "").split()).strip()
        if not replacement and fix:
            # Persist the gap even when the redline LLM skipped this finding: use the FIX
            # as the replacement policy language when possible.
            replacement = fix if fix[:1].isupper() else f"{(anchor or sentence).rstrip('.')} {fix}."
        if not anchor or not replacement or normalize_space_local(anchor) == normalize_space_local(replacement):
            continue
        if is_cover_or_metadata_line(anchor) or is_cover_or_metadata_line(replacement):
            continue
        built.append(
            RedlineEdit(
                anchor_text=anchor[:MAX_EDIT_ANCHOR_CHARS],
                replacement_text=replacement[:MAX_EDIT_REPLACEMENT_CHARS],
                citation_comment=comment[:MAX_CITATION_COMMENT_CHARS],
            )
        )

    for index, edit in enumerate(llm_edits):
        if index in used_llm:
            continue
        anchor = " ".join((edit.anchor_text or "").split()).strip()
        replacement = " ".join((edit.replacement_text or "").split()).strip()
        if not anchor or not replacement or normalize_space_local(anchor) == normalize_space_local(replacement):
            continue
        if is_cover_or_metadata_line(anchor) or is_cover_or_metadata_line(replacement):
            continue
        comment = normalize_rule_gap_fix_comment(edit.citation_comment)
        if not comment:
            comment = format_gap_rule_fix_comment(
                rule=None,
                gap="Policy language at this sentence needs remediation to meet the matching regulation.",
                fix=replacement[:180],
            )
        built.append(
            RedlineEdit(
                anchor_text=anchor[:MAX_EDIT_ANCHOR_CHARS],
                replacement_text=replacement[:MAX_EDIT_REPLACEMENT_CHARS],
                citation_comment=comment[:MAX_CITATION_COMMENT_CHARS],
            )
        )

    if not built:
        return draft
    return RedlineDraft(
        document_title=draft.document_title or context.policy.name,
        drafting_summary=draft.drafting_summary
        or f"Prepared {len(built)} sentence-level redline edit(s) for CCO review.",
        regulatory_sources_used=draft.regulatory_sources_used or [],
        edits=built,
        citation_comment="",
    )


def normalize_space_local(value: str) -> str:
    return " ".join((value or "").split()).strip()


def sentence_findings_for_redline_prompt(gap_analysis: GapAnalysis) -> list[dict[str, str]]:
    from app.services.gap_analysis_service import (
        format_gap_rule_fix_comment,
        sentence_findings_from_metadata,
    )
    from app.services.tracked_redline_document import is_cover_or_metadata_line

    metadata = gap_analysis.retrieval_metadata if isinstance(gap_analysis.retrieval_metadata, dict) else {}
    findings = sentence_findings_from_metadata(metadata)
    if findings:
        return [
            finding
            for finding in findings
            if not is_cover_or_metadata_line(str(finding.get("sentence") or ""))
        ]
    # Legacy single RULE/GAP/FIX row.
    if gap_analysis.gap_summary or gap_analysis.rule or gap_analysis.fix:
        return [
            {
                "sentence": "",
                "rule": str(gap_analysis.rule or "").strip(),
                "gap": str(gap_analysis.gap_summary or "").strip(),
                "fix": str(gap_analysis.fix or "").strip(),
                "impact_score": str(gap_analysis.impact_score or "MEDIUM"),
                "citation_comment": format_gap_rule_fix_comment(
                    rule=gap_analysis.rule,
                    gap=gap_analysis.gap_summary,
                    fix=gap_analysis.fix,
                ),
            }
        ]
    return []


def dedupe_redline_edits(edits: list[RedlineEdit]) -> list[RedlineEdit]:
    """Drop only exact-duplicate comments / identical anchors; keep distinct findings."""
    from app.services.gap_analysis_service import (
        format_gap_rule_fix_comment,
        normalize_rule_gap_fix_comment,
    )
    from app.services.tracked_redline_document import is_cover_or_metadata_line

    cleaned: list[RedlineEdit] = []
    seen_comments: set[str] = set()
    seen_anchors: set[str] = set()

    for edit in edits:
        anchor = " ".join((edit.anchor_text or "").split()).strip()
        replacement = " ".join((edit.replacement_text or "").split()).strip()
        if not anchor or not replacement or anchor == replacement:
            continue
        # Never let the LLM rewrite cover metadata; Next Review Date etc. are system-owned.
        if is_cover_or_metadata_line(anchor) or is_cover_or_metadata_line(replacement):
            continue
        comment = normalize_rule_gap_fix_comment(edit.citation_comment)
        if not comment:
            comment = format_gap_rule_fix_comment(rule=None, gap=None, fix=replacement[:180])
        # Dedupe on collapsed text so equivalent line breaks still count as duplicates.
        comment_key = " ".join(comment.lower().split())
        anchor_key = anchor.lower()
        if comment_key in seen_comments or anchor_key in seen_anchors:
            continue
        seen_comments.add(comment_key)
        seen_anchors.add(anchor_key)
        cleaned.append(
            RedlineEdit(
                anchor_text=anchor[:MAX_EDIT_ANCHOR_CHARS],
                replacement_text=replacement[:MAX_EDIT_REPLACEMENT_CHARS],
                citation_comment=comment[:MAX_CITATION_COMMENT_CHARS],
            )
        )
        if len(cleaned) >= MAX_REDLINE_EDITS:
            break
    return cleaned


def normalize_policy_header(
    header: PolicyDocumentHeader | None,
    context: RedlineContext,
) -> PolicyDocumentHeader:
    regulation = applicable_regulation_from_context(context)
    dates = policy_template_dates()
    defaults = default_policy_header_from_context(
        company_name=context.organization_name,
        policy_name=context.policy.name,
        policy_version=context.policy.version,
        applicable_regulation=regulation,
    )
    doc_version = labeled_metadata_value(context.policy_bytes, "Version")
    doc_regulation = labeled_metadata_value(context.policy_bytes, "Applicable Regulation")
    bumped_version = bump_policy_version_label(
        doc_version or (header.version.strip() if header else ""),
        fallback=str(context.policy.version),
    )
    merged_regulation = merge_applicable_regulation(
        doc_regulation,
        regulation,
        header.applicable_regulation.strip() if header and header.applicable_regulation else "",
    )
    if header is None:
        return PolicyDocumentHeader.model_validate(
            {
                **defaults,
                "version": bumped_version,
                "effective_date": dates["effective_date"],
                "applicable_regulation": merged_regulation,
                "next_review_date": dates["next_review_date"],
            }
        )
    return PolicyDocumentHeader(
        company_name=header.company_name.strip() or defaults["company_name"],
        company_department=header.company_department.strip() or defaults["company_department"],
        policy_name=header.policy_name.strip() or defaults["policy_name"],
        policy_number=header.policy_number.strip() or defaults["policy_number"],
        version=bumped_version,
        effective_date=dates["effective_date"],
        owner=header.owner.strip() or defaults["owner"],
        applicable_regulation=merged_regulation,
        next_review_date=dates["next_review_date"],
    )


def normalize_policy_approval(
    approval: PolicyApprovalFooter | None,
    header: PolicyDocumentHeader,
) -> PolicyApprovalFooter:
    dates = policy_template_dates()
    if approval is None:
        return PolicyApprovalFooter(
            approved_by=DEFAULT_POLICY_APPROVER,
            approved_date=dates["approved_date"],
        )
    return PolicyApprovalFooter(
        approved_by=approval.approved_by.strip() or DEFAULT_POLICY_APPROVER,
        approved_date=dates["approved_date"],
    )


def normalize_policy_sections(
    sections: list[PolicyDocumentSectionRevision],
    *,
    keep_empty_sections: bool = False,
) -> list[PolicyDocumentSectionRevision]:
    normalized: list[PolicyDocumentSectionRevision] = []
    for section in sections:
        body = normalize_space(section.body)
        if contains_meta_prose(body):
            body = ""
        subsections: list[PolicySubsectionRevision] = []
        for subsection in section.subsections:
            sub_body = normalize_space(subsection.body)
            if not sub_body or contains_meta_prose(sub_body):
                continue
            subsections.append(
                PolicySubsectionRevision(
                    number=subsection.number.strip(),
                    title=subsection.title.strip(),
                    body=sub_body,
                )
            )
        if not body and not subsections and not keep_empty_sections:
            continue
        normalized.append(
            PolicyDocumentSectionRevision(
                number=section.number.strip(),
                title=section.title.strip(),
                body=body,
                subsections=subsections,
            )
        )
    return normalized


def legacy_sections_to_template(
    sections: list[PolicySectionRevision],
) -> list[PolicyDocumentSectionRevision]:
    template_sections: list[PolicyDocumentSectionRevision] = []
    for index, section in enumerate(sections, start=1):
        revised = normalize_space(section.revised_text)
        if not revised or contains_meta_prose(revised):
            continue
        template_sections.append(
            PolicyDocumentSectionRevision(
                number=str(index),
                title=(section.heading or f"Section {index}").strip(),
                body=revised,
                subsections=[],
            )
        )
    return template_sections


def applicable_regulation_from_context(context: RedlineContext) -> str:
    citations = context.gap_analysis.regulatory_citations
    if isinstance(citations, list):
        parts: list[str] = []
        for citation in citations[:4]:
            if isinstance(citation, dict):
                source = str(citation.get("source") or "").strip()
                reference = str(citation.get("reference") or "").strip()
                label = ": ".join(part for part in (source, reference) if part)
                if label:
                    parts.append(label)
        if parts:
            return "; ".join(parts)
    regulator = resolve_internal_regulator_source(context.policy.name)
    if regulator:
        return f"{regulator} supervisory requirements"
    return "Applicable regulatory requirements"


def fallback_redline_draft(context: RedlineContext) -> RedlineDraft:
    paragraphs = extract_policy_paragraphs(context.policy_bytes)
    template_sections = template_sections_from_paragraphs(paragraphs)
    if not template_sections:
        legacy = sections_from_policy_paragraphs(
            paragraphs,
            gap_summary=context.gap_analysis.gap_summary,
        )
        template_sections = [
            PolicyDocumentSectionRevision(
                number=str(index),
                title=str(section.get("heading") or f"Section {index}"),
                body=str(section.get("revised_text") or ""),
                subsections=[],
            )
            for index, section in enumerate(legacy, start=1)
            if str(section.get("revised_text") or "").strip()
        ]
    else:
        template_sections = [
            PolicyDocumentSectionRevision.model_validate(section)
            for section in template_sections
        ]

    template_sections = [
        PolicyDocumentSectionRevision.model_validate(section)
        for section in align_policy_sections_to_template(
            [section.model_dump() for section in template_sections]
        )
    ]
    header = PolicyDocumentHeader.model_validate(
        default_policy_header_from_context(
            company_name=context.organization_name,
            policy_name=context.policy.name,
            policy_version=context.policy.version,
            applicable_regulation=applicable_regulation_from_context(context),
        )
    )
    return RedlineDraft(
        document_title=context.policy.name,
        drafting_summary=(
            "Local fallback revision draft created for CCO review because Opus drafting is unavailable."
        ),
        regulatory_sources_used=[
            RegulatorySourceNote(
                policy_name=item.get("policy_name", ""),
                regulator=item.get("regulator", ""),
                excerpt=item.get("excerpt", "")[:MAX_POLICY_CONTEXT_CHARS],
            )
            for item in context.regulatory_references[:6]
        ],
        policy_header=header,
        policy_sections=template_sections,
        approval=PolicyApprovalFooter(
            approved_by=DEFAULT_POLICY_APPROVER,
            approved_date=header.effective_date,
        ),
    )


def contains_meta_prose(replacement_text: str) -> bool:
    lowered = replacement_text.lower()
    return any(phrase in lowered for phrase in META_REPLACEMENT_PHRASES)


def looks_like_rewrite(anchor: str, replacement: str) -> bool:
    anchor_norm = normalize_space(anchor)
    replacement_norm = normalize_space(replacement)
    anchor_words = {
        word.strip(".,;:")
        for word in anchor_norm.lower().split()
        if word.strip(".,;:")
    }
    if not anchor_words:
        return False
    replacement_words = {
        word.strip(".,;:")
        for word in replacement_norm.lower().split()
        if word.strip(".,;:")
    }
    overlap = len(anchor_words & replacement_words) / len(anchor_words)
    if overlap >= 0.6:
        return False
    if len(replacement_norm) > len(anchor_norm) * 2.5 and len(anchor_words) >= 8 and overlap < 0.4:
        return True
    return len(anchor_words) >= 6 and overlap < 0.25


def build_tracked_redline_docx(policy_bytes: bytes, policy_text: str, draft: RedlineDraft) -> bytes:
    """Build a tracked-change DOCX by overlaying sentence edits on the internal policy."""
    from app.services.tracked_redline_document import (
        ParagraphOverlay,
        TrackedReplacement,
        apply_paragraph_overlays,
        apply_tracked_redline_overlay,
        build_metadata_field_overlays_for_internal_policy,
        filter_policy_paragraphs_to_body,
        overlays_from_draft_sections,
        replacements_from_draft_edits,
    )

    regulation_text = regulations_mentioned_in_redline(draft)
    sentence_edits = dedupe_redline_edits(list(draft.edits or []))
    if sentence_edits:
        replacements = [
            TrackedReplacement(
                search_text=edit.anchor_text,
                replace_text=edit.replacement_text,
                comment=edit.citation_comment or None,
            )
            for edit in sentence_edits
        ]
        redline_bytes = apply_tracked_redline_overlay(
            policy_bytes,
            replacements=replacements,
            author=REDLINE_AUTHOR,
            gap_summary="",
        )
        metadata_overlays = build_metadata_field_overlays_for_internal_policy(
            policy_bytes,
            applicable_regulation=regulation_text,
        )
        if metadata_overlays:
            # Apply cover-page metadata updates on the already-redlined document.
            # Seek originals from the source policy so labels still match.
            redline_bytes = apply_paragraph_overlays(
                redline_bytes,
                metadata_overlays,
                author=REDLINE_AUTHOR,
                gap_summary="",
            )
        return redline_bytes

    legacy_replacements = replacements_from_draft_edits(draft)
    if legacy_replacements:
        return apply_tracked_redline_overlay(
            policy_bytes,
            replacements=legacy_replacements,
            author=REDLINE_AUTHOR,
            gap_summary=draft.drafting_summary or "",
        )

    body_overlays: list[ParagraphOverlay] = []
    internal_paragraphs = filter_policy_paragraphs_to_body(
        extract_policy_paragraphs(policy_bytes)
    )
    section_map = overlays_from_draft_sections(internal_paragraphs, draft)
    for index, revised_text in section_map.items():
        if index >= len(internal_paragraphs):
            continue
        original = normalize_space(internal_paragraphs[index].text)
        revised = normalize_space(revised_text)
        if original and revised and original != revised:
            body_overlays.append(
                ParagraphOverlay(original_text=original, revised_text=revised)
            )

    metadata_overlays = build_metadata_field_overlays_for_internal_policy(
        policy_bytes,
        applicable_regulation=regulation_text,
    )
    combined = [*body_overlays, *metadata_overlays]
    if combined:
        return apply_paragraph_overlays(
            policy_bytes,
            combined,
            author=REDLINE_AUTHOR,
            gap_summary="",
        )
    return policy_bytes


def regulations_mentioned_in_redline(draft: RedlineDraft) -> str:
    """Collect rule-name + number labels from every edit RULE comment.

    Applicable Regulation is limited to compact citations such as
    ``FINRA Rule 3310`` — never full RULE prose or act names without numbers.
    """
    from app.services.redline_document import (
        extract_regulation_citations,
        merge_applicable_regulation,
    )

    chunks: list[str] = []
    for edit in draft.edits or []:
        comment = edit.citation_comment or ""
        match = re.search(
            r"(?is)RULE:\s*(.*?)(?:\n\s*\n|\s+GAP:|$)",
            comment,
        )
        rule_text = " ".join(match.group(1).split()).strip() if match else comment
        chunks.extend(extract_regulation_citations(rule_text))

    if draft.policy_header and draft.policy_header.applicable_regulation:
        chunks.extend(
            extract_regulation_citations(draft.policy_header.applicable_regulation)
        )
    return merge_applicable_regulation("", *chunks)


def load_regulatory_reference_sections_for_redline(
    db: Session,
    org_id: str,
    policy: Policy,
    gap_analysis: GapAnalysis,
    policy_text: str,
) -> list[dict[str, str]]:
    """Load REGULATORY_REFERENCE excerpts matched to this internal policy and gap analysis."""
    references: list[dict[str, str]] = []
    seen_chunk_keys: set[str] = set()

    for excerpt in government_excerpts_from_gap(gap_analysis):
        policy_name = str(
            excerpt.get("reference_policy") or excerpt.get("policy_name") or ""
        ).strip()
        regulator = str(
            excerpt.get("regulator_source") or excerpt.get("regulator") or ""
        ).strip()
        text = str(excerpt.get("text") or excerpt.get("content") or excerpt.get("excerpt") or "").strip()
        if not text:
            continue
        chunk_key = f"{policy_name}:{excerpt.get('chunk_index', len(seen_chunk_keys))}"
        if chunk_key in seen_chunk_keys:
            continue
        seen_chunk_keys.add(chunk_key)
        references.append(
            {
                "policy_name": policy_name,
                "regulator": regulator,
                "excerpt": text,
                "match_source": "gap_analysis_retrieval",
            }
        )

    reference_policy = active_reference_policy_for_internal_name(db, org_id, policy.name)
    if reference_policy is not None:
        regulator = (
            resolve_internal_regulator_source(policy.name)
            or regulator_source_from_policy_name(reference_policy.name)
        )
        ranked_excerpts = lexical_government_excerpts(
            db,
            org_id,
            policy_text,
            limit=6,
            policy_ids=[reference_policy.id],
        )
        for ranked in ranked_excerpts:
            chunk_key = f"{ranked.policy_name}:{ranked.chunk_index}"
            if chunk_key in seen_chunk_keys:
                continue
            seen_chunk_keys.add(chunk_key)
            references.append(
                {
                    "policy_name": ranked.policy_name,
                    "regulator": ranked.regulator_source or regulator,
                    "excerpt": ranked.content,
                    "match_source": "internal_policy_topic_match",
                }
            )

        if not any(item["policy_name"] == reference_policy.name for item in references):
            fallback_excerpt = reference_policy_excerpt(db, reference_policy)
            if fallback_excerpt:
                references.append(
                    {
                        "policy_name": reference_policy.name,
                        "regulator": regulator,
                        "excerpt": fallback_excerpt,
                        "match_source": "matched_reference_policy",
                    }
                )
    elif not references:
        references = load_regulatory_reference_sections(db, org_id, limit=1)

    return merge_reference_sections_by_policy(references)


def reference_policy_excerpt(db: Session, reference_policy: Policy) -> str:
    chunks = (
        db.query(DocumentChunk)
        .filter(DocumentChunk.policy_id == reference_policy.id)
        .order_by(DocumentChunk.chunk_index.asc())
        .limit(8)
        .all()
    )
    return "\n\n".join(chunk.content for chunk in chunks if chunk.content).strip()


def merge_reference_sections_by_policy(references: list[dict[str, str]]) -> list[dict[str, str]]:
    grouped: dict[str, dict[str, str]] = {}
    for reference in references:
        policy_name = reference.get("policy_name") or "Reference"
        existing = grouped.get(policy_name)
        excerpt = str(reference.get("excerpt") or "").strip()
        if not excerpt:
            continue
        if existing is None:
            grouped[policy_name] = {
                "policy_name": policy_name,
                "regulator": str(reference.get("regulator") or ""),
                "excerpt": excerpt,
                "match_source": str(reference.get("match_source") or ""),
            }
            continue
        combined = f"{existing['excerpt']}\n\n---\n\n{excerpt}".strip()
        existing["excerpt"] = combined[:MAX_REGULATORY_REFERENCE_CHARS]
    return list(grouped.values())


def load_regulatory_reference_sections(
    db: Session,
    org_id: str,
    *,
    limit: int | None = None,
) -> list[dict[str, str]]:
    """Load active REGULATORY_REFERENCE policy text for revision drafting."""
    references: list[dict[str, str]] = []
    policies = (
        db.query(Regulation)
        .filter(
            Regulation.org_id == org_id,
            Regulation.status == "ACTIVE",
            Regulation.type == REGULATORY_REFERENCE_TYPE,
            Regulation.rule_code.is_(None),
        )
        .order_by(Regulation.name.asc())
        .all()
    )
    for policy in policies:
        chunks = (
            db.query(DocumentChunk)
            .filter(
                (DocumentChunk.regulation_id == policy.id)
                | (
                    (DocumentChunk.policy_id == policy.id)
                    & DocumentChunk.regulation_id.is_(None)
                )
            )
            .order_by(DocumentChunk.chunk_index.asc())
            .all()
        )
        excerpt = "\n\n".join(chunk.content for chunk in chunks).strip()
        if not excerpt:
            continue
        regulator = resolve_internal_regulator_source(policy.name) or policy.name.split()[0].upper()
        references.append(
            {
                "policy_name": policy.name,
                "regulator": regulator,
                "excerpt": excerpt[:MAX_REGULATORY_REFERENCE_CHARS],
                "match_source": "library_fallback",
            }
        )
        if limit is not None and len(references) >= limit:
            break
    return references


async def generate_material_gap_redlines_for_org(org_id: str, db: Session | None = None) -> int:
    """Finalize redline DOCX files for user-requested pending revisions only."""
    del db
    generated = 0
    with session_scope(commit=False) as scoped_db:
        pending_revisions = (
            scoped_db.query(PolicyRevision)
            .join(GapAnalysis, GapAnalysis.id == PolicyRevision.gap_analysis_id)
            .filter(
                PolicyRevision.org_id == org_id,
                PolicyRevision.status == "PENDING",
                PolicyRevision.redline_file_url.is_(None),
                PolicyRevision.review_requested_at.is_not(None),
            )
            .order_by(PolicyRevision.created_at.desc())
            .all()
        )
        pending_jobs = []
        for revision in pending_revisions:
            gap = (
                scoped_db.query(GapAnalysis)
                .filter(GapAnalysis.id == revision.gap_analysis_id)
                .one_or_none()
            )
            if gap is None:
                continue
            if not gap_analysis_eligible_for_redline(
                trigger_type=str(gap.trigger_type or ""),
                impact_score=str(gap.impact_score or ""),
                retrieval_metadata=(
                    gap.retrieval_metadata if isinstance(gap.retrieval_metadata, dict) else None
                ),
            ):
                continue
            pending_jobs.append((revision.id, revision.gap_analysis_id))

    for revision_id, gap_analysis_id in pending_jobs:
        try:
            with session_scope(commit=False) as scoped_db:
                revision = (
                    scoped_db.query(PolicyRevision)
                    .filter(
                        PolicyRevision.id == revision_id,
                        PolicyRevision.org_id == org_id,
                    )
                    .one_or_none()
                )
                if revision is None or revision.redline_file_url:
                    continue
                context = load_redline_context(gap_analysis_id, org_id, scoped_db)
                revision = detach_revision(revision, scoped_db)
                context = detach_redline_context(context, scoped_db)
            await finalize_policy_redline_for_revision(revision, context, db=None)
            generated += 1
        except HTTPException as exc:
            logger.warning(
                "scheduled_revision_finalize_skipped",
                gap_analysis_id=gap_analysis_id,
                org_id=org_id,
                detail=str(exc.detail),
            )
        except Exception as exc:
            logger.warning(
                "scheduled_revision_finalize_failed",
                gap_analysis_id=gap_analysis_id,
                org_id=org_id,
                error_type=type(exc).__name__,
            )
    return generated


def source_policy_text(policy_bytes: bytes) -> str:
    return policy_text_from_paragraphs(extract_policy_paragraphs(policy_bytes))


def citation_comment(gap_analysis: GapAnalysis, draft: RedlineDraft | None = None) -> str:
    """Build a Word-comment RULE/GAP/FIX for each redline tracked change.

    Prefers the redline LLM's ``citation_comment`` when present; otherwise uses the
    full stored gap-analysis RULE/GAP/FIX text (not word-truncated snippets).
    """
    from app.services.gap_analysis_service import (
        format_gap_rule_fix_comment,
        normalize_rule_gap_fix_comment,
    )

    draft_comment = ''
    if draft is not None and (draft.citation_comment or '').strip():
        draft_comment = draft.citation_comment.strip()
    else:
        metadata = gap_analysis.retrieval_metadata if isinstance(gap_analysis.retrieval_metadata, dict) else {}
        draft_comment = str(metadata.get('citation_comment') or '').strip()

    normalized = normalize_rule_gap_fix_comment(draft_comment)
    if normalized.upper().startswith('RULE:') and 'FIX:' in normalized.upper():
        return normalized[:MAX_CITATION_COMMENT_CHARS]

    structured = format_gap_rule_fix_comment(
        rule=getattr(gap_analysis, 'rule', None),
        gap=gap_analysis.gap_summary,
        fix=getattr(gap_analysis, 'fix', None),
    )
    if structured:
        return structured[:MAX_CITATION_COMMENT_CHARS]

    citations = gap_analysis.regulatory_citations if isinstance(gap_analysis.regulatory_citations, list) else []
    parts = [f'GAP: {" ".join(str(gap_analysis.gap_summary or "").split())}'.strip()]
    for citation in citations[:1]:
        if isinstance(citation, dict):
            source = str(citation.get('source') or 'Trigger source')
            reference = str(citation.get('reference') or '')
            parts.insert(0, f'RULE: {" ".join(f"{source} {reference}".split())}'.strip())
            break
    return normalize_rule_gap_fix_comment('\n\n'.join(part for part in parts if part.strip()))[
        :MAX_CITATION_COMMENT_CHARS
    ]


def redline_filename(policy: Policy) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", policy.name).strip("._") or "policy"
    return f"{base}_v{policy.version}_redline.docx"


def is_docx_bytes(value: bytes) -> bool:
    try:
        with ZipFile(BytesIO(value)) as archive:
            return "word/document.xml" in archive.namelist()
    except BadZipFile:
        return False


def government_excerpts_from_gap(gap_analysis: GapAnalysis) -> list[dict[str, Any]]:
    metadata = gap_analysis.retrieval_metadata or {}
    excerpts = metadata.get("government_reference_excerpts")
    if isinstance(excerpts, list):
        return [item for item in excerpts if isinstance(item, dict)]
    return []


def parse_redline_fields_loosely(text: str) -> dict[str, Any] | None:
    """Recover redline draft fields when Opus returns malformed or truncated JSON."""
    replacement_text = extract_json_string_field(text, ("replacement_text",))
    target_excerpt = extract_json_string_field(text, ("target_excerpt",))
    drafting_summary = extract_json_string_field(text, ("drafting_summary",))
    citation_comment_value = extract_json_string_field(text, ("citation_comment",))
    document_title = extract_json_string_field(text, ("document_title",))

    policy_sections = _parse_redline_json_array(text, "policy_sections", _parse_policy_section_object)
    sections = _parse_redline_json_array(text, "sections", _parse_section_object)
    edits = _parse_redline_json_array(text, "edits", _parse_edit_object)
    policy_header = _parse_policy_header_object(text)

    if policy_sections:
        payload: dict[str, Any] = {
            "drafting_summary": truncate_text(drafting_summary, 2_000),
            "citation_comment": truncate_text(citation_comment_value, MAX_CITATION_COMMENT_CHARS),
            "policy_sections": policy_sections,
        }
        if policy_header:
            payload["policy_header"] = policy_header
        approval = _parse_policy_approval_object(text)
        if approval:
            payload["approval"] = approval
        if document_title:
            payload["document_title"] = document_title
        return payload

    if sections:
        return {
            "document_title": document_title,
            "drafting_summary": truncate_text(drafting_summary, 2_000),
            "citation_comment": truncate_text(citation_comment_value, MAX_CITATION_COMMENT_CHARS),
            "sections": sections,
        }

    if edits:
        return {
            "document_title": document_title,
            "drafting_summary": truncate_text(drafting_summary, 2_000),
            "citation_comment": truncate_text(citation_comment_value, MAX_CITATION_COMMENT_CHARS),
            "edits": edits,
        }

    if not replacement_text and not target_excerpt:
        return None

    return {
        "document_title": document_title,
        "drafting_summary": truncate_text(drafting_summary, 2_000),
        "citation_comment": truncate_text(citation_comment_value, MAX_CITATION_COMMENT_CHARS),
        "target_excerpt": truncate_text(target_excerpt, MAX_POLICY_CONTEXT_CHARS),
        "replacement_text": truncate_text(
            replacement_text or target_excerpt,
            MAX_REPLACEMENT_CHARS,
        ),
    }


def _parse_policy_section_object(item: dict[str, Any]) -> dict[str, Any] | None:
    body = str(item.get("body") or item.get("revised_text") or "").strip()
    subsections_raw = item.get("subsections")
    subsections: list[dict[str, str]] = []
    if isinstance(subsections_raw, list):
        for subsection in subsections_raw:
            if not isinstance(subsection, dict):
                continue
            sub_body = str(subsection.get("body") or "").strip()
            if not sub_body:
                continue
            subsections.append(
                {
                    "number": str(subsection.get("number") or ""),
                    "title": str(subsection.get("title") or ""),
                    "body": truncate_text(sub_body, MAX_REPLACEMENT_CHARS),
                }
            )
    if not body and not subsections:
        return None
    return {
        "number": str(item.get("number") or ""),
        "title": str(item.get("title") or item.get("heading") or ""),
        "body": truncate_text(body, MAX_REPLACEMENT_CHARS),
        "subsections": subsections,
    }


def _parse_policy_header_object(text: str) -> dict[str, str] | None:
    header_match = re.search(r'"policy_header"\s*:\s*\{', text)
    if header_match is None:
        return None
    object_start = text.find("{", header_match.start())
    if object_start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[object_start:], object_start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[object_start : index + 1])
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return {str(key): str(value) for key, value in parsed.items()}
                return None
    return None


def _parse_policy_approval_object(text: str) -> dict[str, str] | None:
    approval_match = re.search(r'"approval"\s*:\s*\{', text)
    if approval_match is None:
        return None
    object_start = text.find("{", approval_match.start())
    if object_start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[object_start:], object_start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[object_start : index + 1])
                except json.JSONDecodeError:
                    return None
                if isinstance(parsed, dict):
                    return {str(key): str(value) for key, value in parsed.items()}
                return None
    return None


def _parse_section_object(item: dict[str, Any]) -> dict[str, str] | None:
    original_text = str(item.get("original_text") or "")
    revised_text = str(item.get("revised_text") or item.get("replacement_text") or "")
    if not revised_text.strip():
        return None
    return {
        "heading": str(item.get("heading") or ""),
        "original_text": truncate_text(original_text, MAX_POLICY_CONTEXT_CHARS),
        "revised_text": truncate_text(revised_text, MAX_REPLACEMENT_CHARS),
        "change_note": truncate_text(str(item.get("change_note") or ""), 1_000),
    }


def _parse_edit_object(item: dict[str, Any]) -> dict[str, str] | None:
    anchor = str(item.get("anchor_text") or item.get("target_excerpt") or "")
    replacement = str(item.get("replacement_text") or item.get("replacement") or "")
    if not anchor or not replacement:
        return None
    return {
        "anchor_text": truncate_text(anchor, MAX_EDIT_ANCHOR_CHARS),
        "replacement_text": truncate_text(replacement, MAX_EDIT_REPLACEMENT_CHARS),
    }


def _parse_redline_json_array(
    text: str,
    field_name: str,
    item_parser: Any,
) -> list[dict[str, Any]]:
    match = re.search(rf'"{field_name}"\s*:\s*\[', text)
    if match is None:
        return []

    array_start = text.find("[", match.start())
    if array_start < 0:
        return []

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[array_start:], array_start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                array_blob = text[array_start : index + 1]
                try:
                    parsed = json.loads(repair_json_array(array_blob))
                except json.JSONDecodeError:
                    return []
                if not isinstance(parsed, list):
                    return []
                items: list[dict[str, Any]] = []
                for entry in parsed:
                    if isinstance(entry, dict):
                        parsed_item = item_parser(entry)
                        if parsed_item is not None:
                            items.append(parsed_item)
                return items
                break
    return []


def repair_json_array(text: str) -> str:
    return re.sub(r",\s*]", "]", text)


def policy_revision_response(revision: PolicyRevision) -> PolicyRevisionResponse:
    return PolicyRevisionResponse(
        id=revision.id,
        gap_analysis_id=revision.gap_analysis_id,
        policy_id=revision.policy_id,
        org_id=revision.org_id,
        redline_file_url=revision.redline_file_url,
        status=revision.status,
        reviewer_id=revision.reviewer_id,
        reviewed_at=revision.reviewed_at,
        approved_file_url=revision.approved_file_url,
        created_at=revision.created_at,
        persisted=True,
    )
