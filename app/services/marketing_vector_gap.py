"""Embed submitted marketing copy and official marketing rules, then cosine-filter related excerpts."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.ai import optional_embedding_provider
from app.core.config import Settings
from app.db.session import get_tenant_org_id
from app.services.document_chunk_service import (
    COSINE_RELATEDNESS_FLOOR,
    mean_max_cosine_similarity,
)
from app.services.document_ingestion import TextChunk, chunk_publication_text, generate_embeddings
from app.services.marketing_rule_corpus import (
    RuleDocument,
    fallback_rule_corpus,
    load_marketing_rule_corpus,
)

logger = structlog.get_logger(__name__)

RELATED_COSINE_FLOOR = COSINE_RELATEDNESS_FLOOR
MAX_RELATED_EXCERPTS = 8
MAX_EXCERPT_CHARS = 1_200
LIVE_RULE_CORPUS_TIMEOUT_SECONDS = 12
VECTOR_GAP_FORMATS = frozenset({'webpage', 'presentation', 'social', 'email', 'blog', 'image'})
# Social posts, emails, and image captures should not wait on live eCFR / FINRA downloads.
FALLBACK_RULE_CORPUS_FORMATS = frozenset({'social', 'email', 'text', 'image'})
_EMBED_CACHE: dict[str, list[list[float]]] = {}


@dataclass
class EmbeddedRule:
    document: RuleDocument
    chunks: list[TextChunk]
    vectors: list[list[float]]
    cosine_similarity: float

    def as_metadata_dict(self) -> dict[str, Any]:
        related = self.cosine_similarity >= RELATED_COSINE_FLOOR
        return {
            **self.document.as_metadata_dict(),
            'cosine_similarity': round(self.cosine_similarity, 6),
            'related': related,
            'chunk_count': len(self.vectors),
        }


@dataclass
class MarketingRuleGap:
    content_chunk_count: int
    embedding_model: str
    format_name: str = 'webpage'
    rules: list[EmbeddedRule] = field(default_factory=list)
    related_excerpts: list[dict[str, Any]] = field(default_factory=list)
    slides: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def homepage_chunk_count(self) -> int:
        return self.content_chunk_count

    def as_metadata_dict(self) -> dict[str, Any]:
        return {
            'embedding_model': self.embedding_model,
            'format': self.format_name,
            'content_chunk_count': self.content_chunk_count,
            'homepage_chunk_count': self.content_chunk_count,
            'related_rule_ids': [
                item.document.rule_id for item in self.rules if item.cosine_similarity >= RELATED_COSINE_FLOOR
            ],
            'rules': [item.as_metadata_dict() for item in self.rules],
            'related_excerpt_count': len(self.related_excerpts),
            'slide_count': len(self.slides),
            'slides': self.slides,
            'error': self.error,
        }

    def excerpt_text(self, limit: int = 8_000) -> str:
        parts = [
            f'{item["rule_id"]} (cosine {item["cosine_similarity"]:.3f})\n{item["text"]}'
            for item in self.related_excerpts
        ]
        return '\n\n'.join(parts)[:limit]


HomepageRuleGap = MarketingRuleGap


def primary_marketing_cosine_similarity(rule_versions: Any) -> float | None:
    """Strongest mean-max cosine versus a related marketing-rule document."""
    if not isinstance(rule_versions, dict):
        return None
    payload = rule_versions.get('cosine_similarity')
    if not isinstance(payload, dict):
        return None
    rules = payload.get('rules') or []
    related: list[float] = []
    all_scores: list[float] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        raw = item.get('cosine_similarity')
        if raw is None:
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score != score:
            continue
        all_scores.append(score)
        if item.get('related') is True:
            related.append(score)
    chosen = related or all_scores
    if not chosen:
        return None
    return max(chosen)


def should_analyze_marketing_rule_gap(format_name: str | None) -> bool:
    return str(format_name or '').strip().lower() in VECTOR_GAP_FORMATS


def uses_fallback_rule_corpus(format_name: str | None) -> bool:
    return str(format_name or '').strip().lower() in FALLBACK_RULE_CORPUS_FORMATS


def marketing_gap_embed_text(
    *,
    body_text: str,
    layout_notes: list[dict[str, Any]] | None = None,
    slide_embed_texts: list[str] | None = None,
) -> str:
    parts = [(body_text or '').strip()]
    for note in layout_notes or []:
        if not isinstance(note, dict):
            continue
        for key in ('excerpt', 'text', 'label', 'region', 'font_note', 'diagram_caption'):
            value = str(note.get(key) or '').strip()
            if value and not value.lower().startswith('embedded diagram on slide'):
                parts.append(value)
    for blob in slide_embed_texts or []:
        if blob.strip():
            parts.append(blob.strip())
    return '\n'.join(part for part in parts if part)


def _cache_key(model: str, text: str) -> str:
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]
    return f'{model}:{digest}'


async def _embed_text(title: str, text: str, settings: Settings) -> tuple[list[TextChunk], list[list[float]]]:
    chunks = chunk_publication_text(title, text)
    if not chunks:
        return [], []
    provider = optional_embedding_provider(get_tenant_org_id())
    key = _cache_key(provider.vector_space if provider else '', f'{title}\n{text}')
    cached = _EMBED_CACHE.get(key)
    if cached and len(cached) == len(chunks):
        return chunks, cached
    vectors = await generate_embeddings(chunks, settings)
    if vectors and len(vectors) == len(chunks):
        _EMBED_CACHE[key] = vectors
    return chunks, vectors


def clear_embed_cache() -> None:
    """Drop in-memory rule/content embeddings so a new review re-embeds."""
    _EMBED_CACHE.clear()


def rank_related_excerpts(
    content_vectors: list[list[float]],
    rules: list[EmbeddedRule],
    *,
    top_k: int = MAX_RELATED_EXCERPTS,
    min_score: float = RELATED_COSINE_FLOOR,
) -> list[dict[str, Any]]:
    """Keep rule chunks whose embeddings are interrelated with the content (mean-max cosine)."""
    scored: list[tuple[float, dict[str, Any]]] = []
    for rule in rules:
        for chunk, vector in zip(rule.chunks, rule.vectors, strict=False):
            score = mean_max_cosine_similarity([vector], content_vectors)
            if score < min_score:
                continue
            scored.append((
                score,
                {
                    'rule_id': rule.document.rule_id,
                    'regulator': rule.document.regulator,
                    'title': rule.document.title,
                    'url': rule.document.url,
                    'cosine_similarity': round(score, 6),
                    'text': chunk.content[:MAX_EXCERPT_CHARS],
                },
            ))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in scored[:top_k]]


async def analyze_marketing_rule_gap(
    *,
    content_text: str,
    settings: Settings,
    format_name: str = 'webpage',
    rule_documents: list[RuleDocument] | None = None,
) -> MarketingRuleGap:
    """Embed submitted copy + official rules, then cosine-filter interrelated excerpts."""
    resolved_format = str(format_name or 'webpage').strip().lower() or 'webpage'
    provider = optional_embedding_provider(get_tenant_org_id())
    model = provider.model_label if provider else settings.openai_embedding_model
    empty = MarketingRuleGap(
        content_chunk_count=0,
        embedding_model=model,
        format_name=resolved_format,
    )
    text = (content_text or '').strip()
    if not text:
        empty.error = 'empty_content_text'
        return empty

    content_chunks, content_vectors = await _embed_text(
        f'marketing_{resolved_format}',
        text,
        settings,
    )
    empty.content_chunk_count = len(content_chunks)
    if not content_vectors:
        empty.error = 'content_embedding_unavailable'
        return empty

    if rule_documents is not None:
        documents = rule_documents
    elif uses_fallback_rule_corpus(resolved_format):
        documents = fallback_rule_corpus()
    else:
        try:
            documents = await asyncio.wait_for(
                load_marketing_rule_corpus(settings),
                timeout=LIVE_RULE_CORPUS_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning('marketing_rule_corpus_timeout', format_name=resolved_format)
            documents = fallback_rule_corpus()

    async def _embed_rule(document: RuleDocument) -> EmbeddedRule:
        chunks, vectors = await _embed_text(document.title, document.text, settings)
        if not vectors:
            return EmbeddedRule(document=document, chunks=chunks, vectors=[], cosine_similarity=0.0)
        return EmbeddedRule(
            document=document,
            chunks=chunks,
            vectors=vectors,
            cosine_similarity=mean_max_cosine_similarity(content_vectors, vectors),
        )

    rules = list(await asyncio.gather(*[_embed_rule(document) for document in documents]))

    if not any(item.vectors for item in rules):
        return MarketingRuleGap(
            content_chunk_count=len(content_chunks),
            embedding_model=model,
            format_name=resolved_format,
            rules=rules,
            error='rule_embedding_unavailable',
        )

    related = rank_related_excerpts(content_vectors, [item for item in rules if item.vectors])
    return MarketingRuleGap(
        content_chunk_count=len(content_chunks),
        embedding_model=model,
        format_name=resolved_format,
        rules=rules,
        related_excerpts=related,
    )


async def analyze_presentation_slide_gaps(
    *,
    slides: list[tuple[int, str]],
    settings: Settings,
    rule_documents: list[RuleDocument] | None = None,
    global_text: str = '',
) -> MarketingRuleGap:
    """Embed each slide (text + diagram captions) against SEC/FINRA marketing rules."""
    combined = '\n\n'.join(
        [global_text.strip()] + [blob for _, blob in slides if blob.strip()]
    ).strip()
    gap = await analyze_marketing_rule_gap(
        content_text=combined or global_text,
        settings=settings,
        format_name='presentation',
        rule_documents=rule_documents,
    )
    if not slides or gap.error:
        return gap
    slide_rows: list[dict[str, Any]] = []
    for page, blob in slides:
        if not blob.strip():
            continue
        chunks, vectors = await _embed_text(f'marketing_slide_{page}', blob, settings)
        if not vectors:
            slide_rows.append({
                'page': page,
                'content_chunk_count': len(chunks),
                'related_rule_ids': [],
                'rules': [],
                'error': 'slide_embedding_unavailable',
            })
            continue
        rule_rows = []
        related_ids = []
        for rule in gap.rules:
            if not rule.vectors:
                continue
            score = mean_max_cosine_similarity(vectors, rule.vectors)
            related = score >= RELATED_COSINE_FLOOR
            if related:
                related_ids.append(rule.document.rule_id)
            rule_rows.append({
                'rule_id': rule.document.rule_id,
                'regulator': rule.document.regulator,
                'cosine_similarity': round(score, 6),
                'related': related,
            })
        slide_rows.append({
            'page': page,
            'content_chunk_count': len(chunks),
            'related_rule_ids': related_ids,
            'rules': rule_rows,
        })
    gap.slides = slide_rows
    return gap


async def analyze_homepage_rule_gap(
    *,
    homepage_text: str,
    settings: Settings,
    rule_documents: list[RuleDocument] | None = None,
) -> MarketingRuleGap:
    """Backward-compatible homepage entry point for the shared marketing rule-gap pipeline."""
    return await analyze_marketing_rule_gap(
        content_text=homepage_text,
        format_name='webpage',
        settings=settings,
        rule_documents=rule_documents,
    )


def clear_embedding_cache() -> None:
    _EMBED_CACHE.clear()
