from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.ai import ChatMessage, LLMProvider, optional_llm_provider
from app.core.config import Settings
from app.core.observability import (
    langfuse_workflow_trace,
    normalize_trace_tags,
    trace_event,
)
from app.services.llm_json import (
    extract_json_string_field,
    response_model,
    truncate_text,
)
from app.services.llm_persona import with_financial_law_expert_persona

logger = structlog.get_logger(__name__)

BUSINESS_CHANGE_EVENT_SUMMARY_MAX_TOKENS = 4_096
LLM_BUSINESS_CHANGE_MAX_ATTEMPTS = 3
LLM_BUSINESS_CHANGE_RETRY_DELAY_SECONDS = 1.0
BUSINESS_CHANGE_RATIONALE_MAX_CHARS = 2_000
BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS = 8_000

BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT = with_financial_law_expert_persona(
    """Assess intake for a US financial-services firm's policy-change workflow.

First deeply read and understand the business-change description and change_type end-to-end —
what is changing, who/what is affected, and which compliance domains it touches — before deciding.
Do not decide from keywords alone.

Determine whether an internal business change requires a human policy review. Be conservative: set
triggers_review to true whenever the change could affect regulated activities, supervision, customer
communications, suitability, conflicts, books and records, privacy, operations, staffing
responsibilities, products, markets, or the firm's stated regulatory scope.

Do not draft policy language. Return only a JSON object with exactly these fields:
{"triggers_review": boolean, "rationale": string}

The rationale must be concise, factual, and explain the factors that drove the review decision."""
)

BUSINESS_CHANGE_EVENT_SUMMARY_PROMPT = with_financial_law_expert_persona(
    """Summarize an internal business change for the CCO dashboard.

Given an internal business change and the intake assessment, write a concise event summary that explains
what changed, why it may affect compliance, and what policy review actions are recommended.

Return only JSON with exactly this field:
{"event_summary": string}

Format event_summary as readable Markdown using this structure:
**Business Change:** one short paragraph

**Compliance Impact:** one short intro sentence, then a bullet list using `-` for each impact area

**Recommended Actions:** a numbered list using `1.` `2.` etc.

Use blank lines between sections. Do not collapse the entire summary into one paragraph.
Finish all three sections. Keep the summary factual and grounded in the supplied inputs."""
)


class AssessmentPayload(BaseModel):
    triggers_review: bool
    rationale: str = Field(min_length=1, max_length=BUSINESS_CHANGE_RATIONALE_MAX_CHARS)


class EventSummaryPayload(BaseModel):
    event_summary: str = Field(min_length=1, max_length=BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS)


@dataclass(frozen=True)
class BusinessChangeAssessment:
    triggers_review: bool
    rationale: str
    mode: str
    model: str | None
    event_summary: str = ''


def build_business_change_assessment_prompt(
    *,
    change_type: str,
    description: str,
    change_date: date | None,
    regulatory_scope: dict[str, Any],
) -> str:
    """Serialize assessment inputs so untrusted descriptions cannot alter the system prompt."""
    payload = {
        'business_change': {
            'change_type': change_type,
            'description': description,
            'effective_date': change_date.isoformat() if change_date else None,
        },
        'firm_regulatory_scope': regulatory_scope,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


async def assess_business_change(
    *,
    change_type: str,
    description: str,
    change_date: date | None,
    regulatory_scope: dict[str, Any],
    settings: Settings,
    client: LLMProvider | None = None,
    org_id: str | None = None,
) -> BusinessChangeAssessment:
    """Use Opus when available, otherwise make a deterministic safe local decision."""
    async with langfuse_workflow_trace(
        'business-change-assessment',
        user_id=org_id,
        tags=normalize_trace_tags('business-change-assessment', change_type),
        metadata={'change_type': change_type},
        input_data={
            'change_type': change_type,
            'description_preview': description[:500],
        },
    ):
        return await _assess_business_change_impl(
            change_type=change_type,
            description=description,
            change_date=change_date,
            regulatory_scope=regulatory_scope,
            settings=settings,
            client=client or optional_llm_provider(
                org_id, 'analysis', platform_model=settings.anthropic_opus_model),
        )


async def _assess_business_change_impl(
    *,
    change_type: str,
    description: str,
    change_date: date | None,
    regulatory_scope: dict[str, Any],
    settings: Settings,
    client: LLMProvider | None = None,
) -> BusinessChangeAssessment:
    if client is None:
        triggers_review = change_type.upper() in settings.business_change_review_type_set
        return BusinessChangeAssessment(
            triggers_review=triggers_review,
            rationale=(
                'No AI model is configured; the configured review taxonomy '
                f"{'requires' if triggers_review else 'does not require'} a policy review for {change_type}."
            ),
            mode='RULE_FALLBACK',
            model=None,
        )

    model = client.model_label
    try:
        payload = await request_business_change_assessment(
            client=client,
            settings=settings,
            change_type=change_type,
            description=description,
            change_date=change_date,
            regulatory_scope=regulatory_scope,
        )
        event_summary = await generate_business_change_event_summary(
            change_type=change_type,
            description=description,
            change_date=change_date,
            assessment_rationale=payload.rationale,
            triggers_review=payload.triggers_review,
            regulatory_scope=regulatory_scope,
            settings=settings,
            client=client,
        )
        assessment = BusinessChangeAssessment(
            triggers_review=payload.triggers_review,
            rationale=payload.rationale.strip(),
            mode='LLM',
            model=model,
            event_summary=event_summary,
        )
    except Exception as exc:
        status_code = getattr(exc, 'status_code', None)
        assessment = BusinessChangeAssessment(
            triggers_review=True,
            rationale=(
                'Automated assessment was unavailable; policy review is required '
                'for human evaluation before this change proceeds.'
            ),
            mode='FAIL_SAFE',
            model=model,
        )
        logger.warning(
            'business_change_assessment_failed',
            error_type=type(exc).__name__,
            status_code=status_code,
            model=model,
        )
        trace_event(
            'business_change.assessment_failed',
            {
                'change_type': change_type,
                'error_type': type(exc).__name__,
                'status_code': status_code,
                'model': model,
            },
        )

    trace_event(
        'business_change.assessed',
        {
            'change_type': change_type,
            'triggers_review': assessment.triggers_review,
            'assessment_mode': assessment.mode,
            'model': assessment.model,
        },
    )
    return assessment


async def generate_business_change_event_summary(
    *,
    change_type: str,
    description: str,
    change_date: date | None,
    assessment_rationale: str,
    triggers_review: bool,
    regulatory_scope: dict[str, Any],
    settings: Settings,
    client: LLMProvider | None = None,
) -> str:
    """Generate a CCO-facing event summary (Anthropic Opus in platform mode)."""
    if client is None:
        return (
            f'{change_type.replace("_", " ").title()} business change logged. '
            f'{"Policy review is required." if triggers_review else "No automatic review was flagged."} '
            f'{assessment_rationale}'
        )

    prompt = json.dumps(
        {
            'business_change': {
                'change_type': change_type,
                'description': description,
                'effective_date': change_date.isoformat() if change_date else None,
            },
            'assessment': {
                'triggers_review': triggers_review,
                'rationale': assessment_rationale,
            },
            'firm_regulatory_scope': regulatory_scope,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    last_error: Exception | None = None
    for attempt in range(1, LLM_BUSINESS_CHANGE_MAX_ATTEMPTS + 1):
        try:
            response = await client.generate(
                [ChatMessage('user', prompt)],
                max_tokens=BUSINESS_CHANGE_EVENT_SUMMARY_MAX_TOKENS,
                system=BUSINESS_CHANGE_EVENT_SUMMARY_PROMPT,
                trace_name='business_change.event_summary',
            )
            payload = parse_event_summary_response(response)
            return payload.event_summary.strip()
        except Exception as exc:
            last_error = exc
            logger.warning(
                'business_change_event_summary_failed',
                error_type=type(exc).__name__,
                attempt=attempt,
            )
            if attempt < LLM_BUSINESS_CHANGE_MAX_ATTEMPTS:
                await asyncio.sleep(LLM_BUSINESS_CHANGE_RETRY_DELAY_SECONDS * attempt)
    if last_error is not None:
        logger.warning(
            'business_change_event_summary_exhausted_retries',
            error_type=type(last_error).__name__,
        )
    return assessment_rationale


async def request_business_change_assessment(
    *,
    client: LLMProvider,
    settings: Settings,
    change_type: str,
    description: str,
    change_date: date | None,
    regulatory_scope: dict[str, Any],
) -> AssessmentPayload:
    last_error: Exception | None = None
    prompt = build_business_change_assessment_prompt(
        change_type=change_type,
        description=description,
        change_date=change_date,
        regulatory_scope=regulatory_scope,
    )
    for attempt in range(1, LLM_BUSINESS_CHANGE_MAX_ATTEMPTS + 1):
        try:
            response = await client.generate(
                [ChatMessage('user', prompt)],
                max_tokens=settings.business_change_assessment_max_tokens,
                system=BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT,
                trace_name='business_change.assessment',
            )
            return parse_assessment_response(response)
        except Exception as exc:
            last_error = exc
            logger.warning(
                'business_change_assessment_parse_failed',
                error_type=type(exc).__name__,
                attempt=attempt,
            )
            if attempt < LLM_BUSINESS_CHANGE_MAX_ATTEMPTS:
                await asyncio.sleep(LLM_BUSINESS_CHANGE_RETRY_DELAY_SECONDS * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError('Business change assessment failed without an error')


def parse_assessment_fields_loosely(text: str) -> dict[str, Any] | None:
    rationale = extract_json_string_field(text, ('rationale',))
    if not rationale:
        return None
    lowered = text.lower()
    triggers_review = not (
        '"triggers_review": false' in lowered or '"triggers_review":false' in lowered
    )
    return {
        'triggers_review': triggers_review,
        'rationale': truncate_text(rationale, BUSINESS_CHANGE_RATIONALE_MAX_CHARS),
    }


def parse_event_summary_fields_loosely(text: str) -> dict[str, Any] | None:
    event_summary = extract_json_string_field(text, ('event_summary', 'summary'))
    if not event_summary:
        return None
    return {
        'event_summary': truncate_text(event_summary, BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS),
    }


def parse_assessment_response(response: Any) -> AssessmentPayload:
    """Extract the JSON-only Opus answer and validate its contract."""
    return response_model(
        response,
        AssessmentPayload,
        string_field_limits={'rationale': BUSINESS_CHANGE_RATIONALE_MAX_CHARS},
        loose_parser=parse_assessment_fields_loosely,
        recovery_log_event='business_change_assessment_json_recovered_with_loose_parser',
    )


def parse_event_summary_response(response: Any) -> EventSummaryPayload:
    return response_model(
        response,
        EventSummaryPayload,
        string_field_limits={'event_summary': BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS},
        loose_parser=parse_event_summary_fields_loosely,
        recovery_log_event='business_change_event_summary_json_recovered_with_loose_parser',
    )
