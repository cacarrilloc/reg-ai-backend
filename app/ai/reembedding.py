"""Switch an org's embedding model by re-embedding every stored chunk.

Searches keep using the current vectors while new ones are staged in ``ai_reembed_vectors``.
One transaction swaps them into ``document_chunks`` and the new AI settings are saved right
after. Chunks written while the job ran are then re-embedded in place, once every worker's
settings cache has picked up the new model.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, cast

import structlog
from pgvector.sqlalchemy import Vector
from sqlalchemy import CursorResult, bindparam, exists, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from app.ai.catalog import ModelCatalog, get_model_catalog
from app.ai.credentials import MODE_BYOK, CredentialResolver
from app.ai.errors import user_error_message
from app.ai.litellm_adapter import LiteLLMEmbeddingProvider
from app.ai.store import SETTINGS_CACHE_SECONDS, SETTINGS_FIELDS, AISettingsRecord, AISettingsStore
from app.ai.types import AIConfigurationError, AIError, EmbeddingProvider
from app.core.config import get_settings
from app.db import session as db_session
from app.db.session import platform_session_scope, session_scope
from app.models.core import AIReembedVector, DocumentChunk, OrganizationAIReembedJob

logger = structlog.get_logger(__name__)

STATUS_RUNNING = 'running'
STATUS_SWAPPED = 'swapped'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
ACTIVE_STATUSES = (STATUS_RUNNING, STATUS_SWAPPED)

REEMBED_EVENT = 'ai.embeddings.reembed'
BATCH_SIZE = 64
# Workers may keep embedding with the old model until their settings cache expires.
CATCH_UP_DELAY_SECONDS = SETTINGS_CACHE_SECONDS + 45.0
# A job that stops reporting progress (worker restarted mid-run) may be replaced.
STALE_AFTER = timedelta(minutes=15)
# Finished jobs stay visible in AI settings this long.
RECENT_WINDOW = timedelta(hours=24)
# Chunks added during staging are staged before the swap; give up if uploads never pause.
MAX_SWAP_ROUNDS = 10
SETTINGS_SAVE_ATTEMPTS = 3

_SWAP_SQL = text(
    "update document_chunks as c "
    "set embedding = r.embedding, "
    "metadata = coalesce(c.metadata, '{}'::jsonb) || jsonb_build_object('embedding_model', cast(:label as text)) "
    "from ai_reembed_vectors as r "
    "where r.chunk_id = c.id and r.org_id = :org_id and c.org_id = :org_id"
)
_WRITE_SQL = text(
    "update document_chunks "
    "set embedding = :embedding, "
    "metadata = coalesce(metadata, '{}'::jsonb) || jsonb_build_object('embedding_model', cast(:label as text)) "
    "where id = :chunk_id and org_id = :org_id"
).bindparams(bindparam('embedding', type_=Vector(3072)))


class ReembedInProgress(AIConfigurationError):
    """One embedding-model switch at a time per org."""


def _utcnow() -> datetime:
    return datetime.utcnow()


def _target_json(record: AISettingsRecord) -> dict[str, Any]:
    return {name: getattr(record, name) for name in SETTINGS_FIELDS}


@dataclass(frozen=True)
class ReembedJob:
    id: str
    org_id: str
    status: str
    target: AISettingsRecord
    total_chunks: int
    embedded_chunks: int
    started_at: datetime
    updated_at: datetime
    error: str | None = None
    created_by: str | None = None
    swapped_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def is_stale(self, now: datetime) -> bool:
        return self.active and now - self.updated_at > STALE_AFTER


def _job(row: OrganizationAIReembedJob) -> ReembedJob:
    saved = dict(row.target or {})
    target = AISettingsRecord(
        auth_org_id=row.auth_org_id,
        mode=str(saved.get('mode') or ''),
        generation_provider=saved.get('generation_provider'),
        generation_model=saved.get('generation_model'),
        generation_api_base=saved.get('generation_api_base'),
        embedding_provider=str(saved.get('embedding_provider') or ''),
        embedding_model=str(saved.get('embedding_model') or ''),
        embedding_api_base=saved.get('embedding_api_base'),
        embedding_dimensions=int(saved.get('embedding_dimensions') or 0),
        analysis_provider=saved.get('analysis_provider'),
        analysis_model=saved.get('analysis_model'),
        analysis_api_base=saved.get('analysis_api_base'),
    )
    return ReembedJob(
        id=row.id,
        org_id=row.auth_org_id,
        status=row.status,
        target=target,
        total_chunks=row.total_chunks or 0,
        embedded_chunks=row.embedded_chunks or 0,
        started_at=row.started_at,
        updated_at=row.updated_at,
        error=row.error,
        created_by=row.created_by,
        swapped_at=row.swapped_at,
        finished_at=row.finished_at,
    )


def _is_missing_table(exc: ProgrammingError) -> bool:
    return getattr(exc.orig, 'sqlstate', None) == '42P01'


class ReembedStore:
    """Job rows on the control plane; staged vectors in the org's own data plane."""

    def latest(self, org_id: str) -> ReembedJob | None:
        if db_session.SessionLocal is None:
            return None
        try:
            with platform_session_scope(commit=False) as db:
                row = (
                    db.query(OrganizationAIReembedJob)
                    .filter(OrganizationAIReembedJob.auth_org_id == org_id)
                    .order_by(OrganizationAIReembedJob.started_at.desc())
                    .first()
                )
                return _job(row) if row else None
        except ProgrammingError as exc:
            # Before migration 20260927_0046 no org can have a re-embed job.
            if not _is_missing_table(exc):
                raise
            return None

    def load(self, job_id: str) -> ReembedJob | None:
        with platform_session_scope(commit=False) as db:
            row = db.get(OrganizationAIReembedJob, job_id)
            return _job(row) if row else None

    def create(self, org_id: str, target: AISettingsRecord, *, total: int, actor_id: str) -> ReembedJob:
        now = _utcnow()
        try:
            with platform_session_scope() as db:
                active = (
                    db.query(OrganizationAIReembedJob)
                    .filter(
                        OrganizationAIReembedJob.auth_org_id == org_id,
                        OrganizationAIReembedJob.status.in_(ACTIVE_STATUSES),
                    )
                    .with_for_update()
                    .all()
                )
                for row in active:
                    if not _job(row).is_stale(now):
                        raise ReembedInProgress('Documents are already being re-embedded for a model switch.')
                    row.status = STATUS_FAILED
                    row.error = 'Re-embedding stopped responding.'
                    row.finished_at = now
                    row.updated_at = now
                db.flush()
                row = OrganizationAIReembedJob(
                    id=str(uuid.uuid4()),
                    auth_org_id=org_id,
                    status=STATUS_RUNNING,
                    target=_target_json(target),
                    total_chunks=total,
                    embedded_chunks=0,
                    created_by=actor_id,
                    started_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.flush()
                job = _job(row)
        except IntegrityError as exc:
            raise ReembedInProgress('Documents are already being re-embedded for a model switch.') from exc
        return job

    def update(self, job_id: str, **fields: Any) -> None:
        with platform_session_scope() as db:
            row = db.get(OrganizationAIReembedJob, job_id)
            if row is None:
                return
            for name, value in fields.items():
                setattr(row, name, value)
            row.updated_at = _utcnow()

    def clear_staged(self, org_id: str) -> None:
        with session_scope(org_id=org_id) as db:
            db.query(AIReembedVector).filter(AIReembedVector.org_id == org_id).delete(synchronize_session=False)

    def unstaged_chunks(self, org_id: str, limit: int) -> list[tuple[str, str]]:
        staged = exists().where(AIReembedVector.chunk_id == DocumentChunk.id)
        with session_scope(commit=False, org_id=org_id) as db:
            rows = (
                db.query(DocumentChunk.id, DocumentChunk.content)
                .filter(DocumentChunk.org_id == org_id, DocumentChunk.embedding.is_not(None), ~staged)
                .order_by(DocumentChunk.id)
                .limit(limit)
                .all()
            )
            return [(row.id, row.content) for row in rows]

    def stage(self, org_id: str, vectors: Sequence[tuple[str, list[float]]]) -> None:
        if not vectors:
            return
        stmt = insert(AIReembedVector).values(
            [{'chunk_id': chunk_id, 'org_id': org_id, 'embedding': vector} for chunk_id, vector in vectors])
        with session_scope(org_id=org_id) as db:
            db.execute(stmt.on_conflict_do_update(
                index_elements=[AIReembedVector.chunk_id], set_={'embedding': stmt.excluded.embedding}))

    def swap(self, org_id: str, label: str) -> int | None:
        """Replace every vector with its staged one in one transaction.

        ``None`` (nothing changed) when some chunk has no staged vector yet.
        """
        staged = exists().where(AIReembedVector.chunk_id == DocumentChunk.id)
        with session_scope(org_id=org_id) as db:
            pending = (
                db.query(DocumentChunk.id)
                .filter(DocumentChunk.org_id == org_id, DocumentChunk.embedding.is_not(None), ~staged)
                .first()
            )
            if pending is not None:
                return None
            result = cast(CursorResult[Any], db.execute(_SWAP_SQL, {'label': label, 'org_id': org_id}))
            db.query(AIReembedVector).filter(AIReembedVector.org_id == org_id).delete(synchronize_session=False)
            return int(result.rowcount or 0)

    def chunks_since(self, org_id: str, since: datetime, *, after_id: str, limit: int) -> list[tuple[str, str]]:
        with session_scope(commit=False, org_id=org_id) as db:
            rows = (
                db.query(DocumentChunk.id, DocumentChunk.content)
                .filter(
                    DocumentChunk.org_id == org_id,
                    DocumentChunk.embedding.is_not(None),
                    DocumentChunk.created_at >= since,
                    DocumentChunk.id > after_id,
                )
                .order_by(DocumentChunk.id)
                .limit(limit)
                .all()
            )
            return [(row.id, row.content) for row in rows]

    def write(self, org_id: str, vectors: Sequence[tuple[str, list[float]]], *, label: str) -> None:
        with session_scope(org_id=org_id) as db:
            for chunk_id, vector in vectors:
                db.execute(_WRITE_SQL, {'embedding': vector, 'label': label, 'chunk_id': chunk_id, 'org_id': org_id})


class ReembedRunner:
    def __init__(
        self,
        *,
        jobs: ReembedStore | None = None,
        settings_store: AISettingsStore | None = None,
        catalog: ModelCatalog | None = None,
        resolver: CredentialResolver | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        batch_size: int = BATCH_SIZE,
        catch_up_delay: float = CATCH_UP_DELAY_SECONDS,
    ) -> None:
        self._jobs = jobs or ReembedStore()
        self._settings_store = settings_store or AISettingsStore()
        self._catalog = catalog or get_model_catalog()
        self._resolver = resolver or CredentialResolver(catalog=self._catalog, store=self._settings_store)
        self._sleep = sleep
        self._batch_size = batch_size
        self._catch_up_delay = catch_up_delay

    def _provider(self, job: ReembedJob) -> EmbeddingProvider:
        target = job.target
        spec = self._catalog.resolve(
            target.embedding_provider, target.embedding_model, 'embedding',
            api_base=target.embedding_api_base, dimensions=target.embedding_dimensions,
        )
        return LiteLLMEmbeddingProvider(
            self._resolver.bind(spec, org_id=job.org_id, byok=target.mode == MODE_BYOK))

    async def _embed(
        self, provider: EmbeddingProvider, chunks: Sequence[tuple[str, str]],
    ) -> list[tuple[str, list[float]]]:
        result = await provider.embed([content.strip() or ' ' for _, content in chunks], trace_name='ai.reembed')
        return [(chunk_id, vector) for (chunk_id, _), vector in zip(chunks, result.vectors, strict=True)]

    async def _stage_and_swap(self, job: ReembedJob, provider: EmbeddingProvider) -> int:
        done = 0
        for _ in range(MAX_SWAP_ROUNDS):
            while batch := self._jobs.unstaged_chunks(job.org_id, self._batch_size):
                self._jobs.stage(job.org_id, await self._embed(provider, batch))
                done += len(batch)
                self._jobs.update(job.id, embedded_chunks=done, total_chunks=max(job.total_chunks, done))
            swapped = self._jobs.swap(job.org_id, provider.model_label)
            if swapped is not None:
                return swapped
        raise AIConfigurationError('Documents kept changing while re-embedding. Try again when uploads finish.')

    async def _save_settings(self, job: ReembedJob) -> None:
        for attempt in range(1, SETTINGS_SAVE_ATTEMPTS + 1):
            try:
                self._settings_store.save_settings(job.target, actor_id=job.created_by or 'system')
                return
            except SQLAlchemyError:
                if attempt == SETTINGS_SAVE_ATTEMPTS:
                    raise
                await self._sleep(2.0 * attempt)

    async def _catch_up(self, job: ReembedJob, provider: EmbeddingProvider) -> int:
        refreshed, after_id = 0, ''
        while batch := self._jobs.chunks_since(job.org_id, job.started_at, after_id=after_id, limit=self._batch_size):
            self._jobs.write(job.org_id, await self._embed(provider, batch), label=provider.model_label)
            refreshed += len(batch)
            after_id = batch[-1][0]
        return refreshed

    async def run(self, job_id: str) -> str:
        job = self._jobs.load(job_id)
        if job is None:
            return 'missing'
        if not job.active:
            return job.status
        log = logger.bind(job_id=job.id, org_id=job.org_id, embedding_model=job.target.embedding_model)
        swapped = job.status == STATUS_SWAPPED
        try:
            provider = self._provider(job)
            if not swapped:
                count = await self._stage_and_swap(job, provider)
                swapped = True
                await self._save_settings(job)
                self._jobs.update(job.id, status=STATUS_SWAPPED, swapped_at=_utcnow())
                log.info('ai_reembed_swapped', chunks=count)
            await self._sleep(self._catch_up_delay)
            refreshed = await self._catch_up(job, provider)
            self._jobs.update(job.id, status=STATUS_COMPLETED, finished_at=_utcnow())
            log.info('ai_reembed_completed', refreshed_recent_chunks=refreshed)
            return STATUS_COMPLETED
        except Exception as exc:
            log.exception('ai_reembed_failed', swapped=swapped, error_type=type(exc).__name__)
            message = user_error_message(exc) if isinstance(exc, AIError) else 'Re-embedding stopped unexpectedly.'
            if swapped:
                message = f'Switched to the new model, but recent uploads may need re-uploading: {message}'
            else:
                try:
                    self._jobs.clear_staged(job.org_id)
                except SQLAlchemyError:
                    log.warning('ai_reembed_clear_staged_failed')
            self._jobs.update(job.id, status=STATUS_FAILED, error=message[:500], finished_at=_utcnow())
            return STATUS_FAILED


def visible_job(job: ReembedJob | None, *, now: datetime | None = None) -> ReembedJob | None:
    """The job AI settings should show: in flight, or finished recently. Stale runs read as failed."""
    if job is None:
        return None
    now = now or _utcnow()
    if job.is_stale(now):
        return replace(job, status=STATUS_FAILED, error='Re-embedding stopped responding. Start the switch again.')
    if job.active or (job.finished_at and now - job.finished_at < RECENT_WINDOW):
        return job
    return None


async def _send_inngest_event(job_id: str) -> bool:
    settings = get_settings()
    if not settings.inngest_async_dispatch_enabled:
        return False
    try:
        import inngest

        from app.core.inngest import create_inngest_client
    except ImportError:
        return False
    try:
        await create_inngest_client(settings).send(inngest.Event(name=REEMBED_EVENT, data={'job_id': job_id}))
        return True
    except Exception as exc:
        logger.warning('ai_reembed_event_failed', job_id=job_id, error_type=type(exc).__name__)
        return False


async def dispatch_reembed_job(job_id: str) -> None:
    """Inngest when configured (production); otherwise run in this process."""
    if await _send_inngest_event(job_id):
        logger.info('ai_reembed_queued_inngest', job_id=job_id)
        return
    await ReembedRunner().run(job_id)
