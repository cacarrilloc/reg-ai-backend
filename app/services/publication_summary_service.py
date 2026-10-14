"""LLM summaries for the Regulatory Feed Publications table."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.ai import ChatMessage, LLMProvider, optional_llm_provider
from app.core.config import Settings
from app.models.core import RegulatoryPublication
from app.services.llm_json import extract_json_string_field, response_model, truncate_text
from app.services.llm_persona import with_financial_law_expert_persona

logger = structlog.get_logger(__name__)

# Enough room for a complete CCO brief. 2000 chars cut mid-sentence and added “…”.
PUBLICATION_FEED_SUMMARY_MAX_CHARS = 8_000
PUBLICATION_FEED_SUMMARY_MAX_TOKENS = 2_500
PUBLICATION_FEED_SUMMARY_BACKFILL_LIMIT = 40
# Older ~1000-character summaries are rewritten on the next ingest.
PUBLICATION_FEED_SUMMARY_REFRESH_BELOW_CHARS = 1_200
# Below this, the Regulatory Feed hides Show more and the text is usually the title.
PUBLICATION_BODY_MIN_USEFUL_CHARS = 220
LLM_PUBLICATION_SUMMARY_MAX_ATTEMPTS = 3
LLM_PUBLICATION_SUMMARY_RETRY_DELAY_SECONDS = 1.0
PUBLICATION_SUMMARY_SOURCE_CHARS = 20_000

PUBLICATION_FEED_SUMMARY_PROMPT = with_financial_law_expert_persona(
    """Summarize a US financial-regulator publication for CCO readers of a monitoring feed.

Write a complete standalone brief — typically 250–400 words. Cover:
- what the agency announced, ordered, or published
- who or what is affected
- key facts present in the source (amounts, dates, products, rules)
- why a broker-dealer or investment-adviser CCO should care

Finish every sentence. Do not end with an ellipsis or cut off mid-thought.
Do not merely repeat the title. Do not invent facts that are not in the source.
If the source is thin, say only what the title and body support.

Return JSON only:
{"summary": string}"""
)


class PublicationFeedSummaryPayload(BaseModel):
    summary: str = Field(min_length=1, max_length=PUBLICATION_FEED_SUMMARY_MAX_CHARS)


def parse_publication_feed_summary_fields_loosely(text: str) -> dict[str, Any] | None:
    summary = extract_json_string_field(text, ('summary',))
    if not summary:
        return None
    return {'summary': truncate_text(summary, PUBLICATION_FEED_SUMMARY_MAX_CHARS)}


def parse_publication_feed_summary_response(response: Any) -> PublicationFeedSummaryPayload:
    return response_model(
        response,
        PublicationFeedSummaryPayload,
        string_field_limits={'summary': PUBLICATION_FEED_SUMMARY_MAX_CHARS},
        loose_parser=parse_publication_feed_summary_fields_loosely,
        recovery_log_event='publication_feed_summary_json_recovered_with_loose_parser',
    )


def fallback_publication_excerpt(
    raw_text: str | None,
    *,
    limit: int = PUBLICATION_FEED_SUMMARY_MAX_CHARS,
) -> str | None:
    text = re.sub(r'\s+', ' ', raw_text or '').strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def publication_text_is_title_only(raw_text: str | None, title: str | None = None) -> bool:
    """True when the body is only the headline (common for CFTC RSS)."""
    text = re.sub(r'\s+', ' ', raw_text or '').strip()
    title_text = re.sub(r'\s+', ' ', (title or '').strip())
    return bool(text and title_text and text.casefold() == title_text.casefold())


def publication_text_is_thin(raw_text: str | None, title: str | None = None) -> bool:
    """True when stored text is empty, title-only, or too short to expand in the feed."""
    text = re.sub(r'\s+', ' ', raw_text or '').strip()
    if not text:
        return True
    if publication_text_is_title_only(text, title):
        return True
    return len(text) < PUBLICATION_BODY_MIN_USEFUL_CHARS


def feed_summary_looks_clipped(summary: str | None) -> bool:
    """True when the stored brief was hard-cut (old 2000-char cap added '...')."""
    stripped = (summary or '').rstrip()
    return stripped.endswith('…') or stripped.endswith('...')


def feed_summary_needs_refresh(summary: str | None) -> bool:
    """True when the stored summary is empty, too short, or cut off mid-sentence."""
    stripped = (summary or '').strip()
    if len(stripped) < PUBLICATION_FEED_SUMMARY_REFRESH_BELOW_CHARS:
        return True
    return feed_summary_looks_clipped(stripped)


def should_replace_feed_summary(
    existing: str | None,
    incoming: str | None,
    title: str | None = None,
) -> bool:
    """Keep a long clipped brief rather than replacing it with a thin excerpt."""
    summary = (incoming or '').strip()
    if not summary or feed_summary_looks_clipped(summary):
        return False
    stored = (existing or '').strip()
    if (
        publication_text_is_thin(summary, title)
        and stored
        and not publication_text_is_thin(stored, title)
    ):
        return False
    return not stored or feed_summary_needs_refresh(stored)


async def generate_publication_feed_summary(
    *,
    title: str,
    source: str,
    url: str,
    raw_text: str | None,
    settings: Settings,
    client: LLMProvider | None = None,
) -> str | None:
    """Write a complete reader brief, or None when the model cannot run."""
    excerpt = fallback_publication_excerpt(raw_text)
    if client is None:
        # Shared regulatory feed: always Reg-AI's platform model.
        client = optional_llm_provider(None, 'analysis', platform_model=settings.anthropic_sonnet_model)
    if client is None:
        return excerpt

    body = (raw_text or '').strip()[:PUBLICATION_SUMMARY_SOURCE_CHARS]
    prompt = json.dumps(
        {
            'source': source,
            'title': title,
            'url': url,
            'publication_text': body or title,
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    last_error: Exception | None = None
    for attempt in range(1, LLM_PUBLICATION_SUMMARY_MAX_ATTEMPTS + 1):
        try:
            response = await client.generate(
                [ChatMessage('user', prompt)],
                max_tokens=PUBLICATION_FEED_SUMMARY_MAX_TOKENS,
                system=PUBLICATION_FEED_SUMMARY_PROMPT,
                throttle='sonnet',
                throttle_budget=False,
                trace_name='regulatory.publication_feed_summary',
            )
            payload = parse_publication_feed_summary_response(response)
            summary = payload.summary.strip()
            if feed_summary_looks_clipped(summary):
                raise ValueError('publication feed summary ended with an ellipsis')
            return summary
        except Exception as exc:
            last_error = exc
            logger.warning(
                'publication_feed_summary_failed',
                error_type=type(exc).__name__,
                attempt=attempt,
                title=title[:120],
            )
            if attempt < LLM_PUBLICATION_SUMMARY_MAX_ATTEMPTS:
                await asyncio.sleep(LLM_PUBLICATION_SUMMARY_RETRY_DELAY_SECONDS * attempt)
    if last_error is not None:
        logger.warning(
            'publication_feed_summary_exhausted_retries',
            error_type=type(last_error).__name__,
        )
    return excerpt


def list_publications_missing_feed_summaries(
    db: Session,
    *,
    limit: int = PUBLICATION_FEED_SUMMARY_BACKFILL_LIMIT,
) -> list[RegulatoryPublication]:
    return (
        db.query(RegulatoryPublication)
        .filter(
            or_(
                RegulatoryPublication.feed_summary.is_(None),
                RegulatoryPublication.feed_summary == '',
                func.length(RegulatoryPublication.feed_summary)
                < PUBLICATION_FEED_SUMMARY_REFRESH_BELOW_CHARS,
                RegulatoryPublication.feed_summary.like('%...'),
                RegulatoryPublication.feed_summary.like('%…'),
            )
        )
        .order_by(
            RegulatoryPublication.published_at.desc().nullslast(),
            RegulatoryPublication.created_at.desc(),
        )
        .limit(limit)
        .all()
    )


async def fill_publication_feed_summaries(
    db: Session,
    publications: Sequence[RegulatoryPublication],
    settings: Settings,
    *,
    client: LLMProvider | None = None,
) -> int:
    """Generate and persist missing feed summaries. Safe to call with an empty list."""
    pending = [
        publication
        for publication in publications
        if feed_summary_needs_refresh(publication.feed_summary)
    ]
    if not pending:
        return 0

    written = 0
    for publication in pending:
        summary = await generate_publication_feed_summary(
            title=publication.title,
            source=publication.source,
            url=publication.url,
            raw_text=publication.raw_text,
            settings=settings,
            client=client,
        )
        if not should_replace_feed_summary(
            publication.feed_summary, summary, publication.title
        ):
            continue
        publication.feed_summary = summary
        written += 1
    if written:
        db.commit()
    return written
