"""Persistence for org AI settings (control plane) and encrypted provider keys (tenant schema)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime

import structlog
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from app.ai.crypto import credential_context, decrypt_secret, encrypt_secret
from app.db import session as db_session
from app.db.session import TenantSchemaNotReady, platform_session_scope, session_scope
from app.models.core import AIProviderCredential, DocumentChunk, OrganizationAISettings

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AISettingsRecord:
    auth_org_id: str
    mode: str
    generation_provider: str | None
    generation_model: str | None
    generation_api_base: str | None
    embedding_provider: str
    embedding_model: str
    embedding_api_base: str | None
    embedding_dimensions: int
    # Null analysis means this org still uses the redline (generation) model for analysis.
    analysis_provider: str | None = None
    analysis_model: str | None = None
    analysis_api_base: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class CredentialStatus:
    provider: str
    key_hint: str
    updated_at: datetime | None
    provider_name: str = ''
    api_base: str | None = None
    enabled_models: tuple[str, ...] = ()


SETTINGS_FIELDS = (
    'mode', 'generation_provider', 'generation_model', 'generation_api_base',
    'analysis_provider', 'analysis_model', 'analysis_api_base',
    'embedding_provider', 'embedding_model', 'embedding_api_base', 'embedding_dimensions',
)


def _record(row: OrganizationAISettings) -> AISettingsRecord:
    return AISettingsRecord(
        auth_org_id=row.auth_org_id,
        updated_at=row.updated_at,
        **{name: getattr(row, name) for name in SETTINGS_FIELDS},
    )


def _credential_query(db, org_id: str, provider: str):
    return db.query(AIProviderCredential).filter(
        AIProviderCredential.org_id == org_id,
        AIProviderCredential.provider == provider,
    )


def _status(row: AIProviderCredential) -> CredentialStatus:
    return CredentialStatus(
        provider=row.provider,
        key_hint=row.key_hint,
        updated_at=row.updated_at,
        provider_name=row.provider_name,
        api_base=row.api_base,
        enabled_models=tuple(row.enabled_models or ()),
    )


def _is_missing_table(exc: ProgrammingError) -> bool:
    return getattr(exc.orig, 'sqlstate', None) == '42P01'


def _tenant_schema_ready(org_id: str) -> bool:
    """True when provider keys can be written to the org's dedicated schema."""
    if db_session.SessionLocal is None:
        return False
    try:
        db_session.prepare_org_data_plane(org_id)
    except TenantSchemaNotReady:
        return False
    return bool(db_session.resolve_schema_for_org(org_id))


def _public_credential_payloads(org_id: str) -> list[dict]:
    with platform_session_scope(commit=False) as db:
        rows = (
            db.query(AIProviderCredential)
            .filter(AIProviderCredential.org_id == org_id)
            .all()
        )
        return [
            {
                'id': row.id,
                'org_id': row.org_id,
                'provider': row.provider,
                'provider_name': row.provider_name,
                'auth_key_ciphertext': row.auth_key_ciphertext,
                'key_hint': row.key_hint,
                'api_base': row.api_base,
                'enabled_models': list(row.enabled_models or []),
                'updated_by': row.updated_by,
                'created_at': row.created_at,
                'updated_at': row.updated_at,
            }
            for row in rows
        ]


def _status_from_payload(payload: dict) -> CredentialStatus:
    return CredentialStatus(
        provider=payload['provider'],
        key_hint=payload['key_hint'],
        updated_at=payload.get('updated_at'),
        provider_name=payload.get('provider_name') or '',
        api_base=payload.get('api_base'),
        enabled_models=tuple(payload.get('enabled_models') or ()),
    )


def _upsert_credential_row(
    db, org_id: str, provider: str, *, ciphertext: str, hint: str, actor_id: str,
    provider_name: str, api_base: str | None, now: datetime,
) -> CredentialStatus:
    row = _credential_query(db, org_id, provider).one_or_none()
    if row is None:
        row = AIProviderCredential(
            id=str(uuid.uuid4()), org_id=org_id, provider=provider, enabled_models=[], created_at=now)
        db.add(row)
    row.provider_name = provider_name or provider
    row.auth_key_ciphertext = ciphertext
    row.key_hint = hint
    row.api_base = api_base
    row.updated_by = actor_id
    row.updated_at = now
    db.flush()
    return _status(row)


def _merge_public_credentials(
    org_id: str, tenant_statuses: list[CredentialStatus],
) -> list[CredentialStatus]:
    """Bring keys saved to public during org creation into the tenant schema.

    Heal even when the tenant already has another provider: adding an OpenAI key
    in Settings used to skip the Anthropic key left in public.
    """
    payloads = _public_credential_payloads(org_id)
    if not payloads:
        return tenant_statuses
    have = {status.provider for status in tenant_statuses}
    missing = [payload for payload in payloads if payload['provider'] not in have]
    if not db_session.resolve_schema_for_org(org_id):
        by_provider = {status.provider: status for status in tenant_statuses}
        for payload in payloads:
            by_provider.setdefault(payload['provider'], _status_from_payload(payload))
        return sorted(by_provider.values(), key=lambda status: status.provider)
    if not missing:
        with platform_session_scope() as db:
            db.query(AIProviderCredential).filter(AIProviderCredential.org_id == org_id).delete(
                synchronize_session=False)
        return tenant_statuses
    with session_scope(org_id=org_id) as db:
        for payload in missing:
            if _credential_query(db, org_id, payload['provider']).one_or_none() is None:
                db.add(AIProviderCredential(**payload))
        # Sessions are autoflush=False. A SELECT here would otherwise run before
        # the INSERT, and Settings would say the key is missing on the same request
        # that stored it.
        db.flush()
        rows = (
            db.query(AIProviderCredential)
            .filter(AIProviderCredential.org_id == org_id)
            .order_by(AIProviderCredential.provider)
            .all()
        )
        merged = [_status(row) for row in rows]
    confirmed = {status.provider for status in merged}
    for payload in missing:
        if payload['provider'] not in confirmed:
            merged.append(_status_from_payload(payload))
    merged.sort(key=lambda status: status.provider)
    copied = [payload['provider'] for payload in missing if payload['provider'] in confirmed]
    if copied:
        with platform_session_scope() as db:
            db.query(AIProviderCredential).filter(
                AIProviderCredential.org_id == org_id,
                AIProviderCredential.provider.in_(copied),
            ).delete(synchronize_session=False)
    logger.info(
        'ai_credentials_moved_to_tenant',
        org_id=org_id,
        providers=[payload['provider'] for payload in missing],
    )
    return merged


# Workflows resolve settings on every AI call; other workers may lag a save by this TTL.
SETTINGS_CACHE_SECONDS = 15.0
_settings_cache: dict[str, tuple[float, AISettingsRecord | None]] = {}


def clear_settings_cache() -> None:
    _settings_cache.clear()


class AISettingsStore:
    def load_settings(self, org_id: str) -> AISettingsRecord | None:
        cached = _settings_cache.get(org_id)
        if cached and time.monotonic() - cached[0] < SETTINGS_CACHE_SECONDS:
            return cached[1]
        record = self._load_settings(org_id)
        _settings_cache[org_id] = (time.monotonic(), record)
        return record

    def _load_settings(self, org_id: str) -> AISettingsRecord | None:
        if db_session.SessionLocal is None:
            return None  # Database-less local mode runs on platform defaults.
        try:
            with platform_session_scope(commit=False) as db:
                row = db.get(OrganizationAISettings, org_id)
                return _record(row) if row else None
        except ProgrammingError as exc:
            # Before migration 20260924_0045 no org can have saved settings.
            if not _is_missing_table(exc):
                raise
            logger.warning('ai_settings_table_missing', org_id=org_id)
            return None

    def save_settings(self, record: AISettingsRecord, *, actor_id: str) -> AISettingsRecord:
        now = datetime.utcnow()
        with platform_session_scope() as db:
            row = db.get(OrganizationAISettings, record.auth_org_id)
            if row is None:
                row = OrganizationAISettings(auth_org_id=record.auth_org_id, created_at=now)
                db.add(row)
            for name in SETTINGS_FIELDS:
                setattr(row, name, getattr(record, name))
            row.updated_by = actor_id
            row.updated_at = now
            db.flush()
            saved = _record(row)
        _settings_cache.pop(record.auth_org_id, None)
        logger.info('ai_settings_saved', org_id=record.auth_org_id, actor_id=actor_id, mode=record.mode,
                    generation_provider=record.generation_provider, embedding_provider=record.embedding_provider)
        return saved

    def embedded_chunk_count(self, org_id: str) -> int:
        """Stored vectors in the org's data plane; switching models means re-embedding these."""
        if db_session.SessionLocal is None:
            return 0
        try:
            with session_scope(commit=False, org_id=org_id) as db:
                return db.query(DocumentChunk.id).filter(
                    DocumentChunk.org_id == org_id,
                    DocumentChunk.embedding.is_not(None),
                ).count()
        except TenantSchemaNotReady:
            return 0

    # Provider keys are data plane: they live in the org's own tenant schema.

    def ensure_encryption_available(self) -> None:
        """Raise ``AIEncryptionUnavailable`` when keys cannot be encrypted at rest."""
        encrypt_secret('', context='preflight')

    def credential_statuses(self, org_id: str) -> list[CredentialStatus]:
        if db_session.SessionLocal is None:
            return []
        statuses: list[CredentialStatus] = []
        try:
            with session_scope(commit=False, org_id=org_id) as db:
                rows = (
                    db.query(AIProviderCredential)
                    .filter(AIProviderCredential.org_id == org_id)
                    .order_by(AIProviderCredential.provider)
                    .all()
                )
                statuses = [_status(r) for r in rows]
        except TenantSchemaNotReady:
            statuses = []
        except ProgrammingError as exc:
            if not _is_missing_table(exc):
                raise
            logger.warning('ai_credentials_table_missing', org_id=org_id)
            statuses = []
        try:
            return _merge_public_credentials(org_id, statuses)
        except (TenantSchemaNotReady, ProgrammingError, SQLAlchemyError):
            logger.warning('ai_credentials_tenant_heal_failed', org_id=org_id)
            try:
                have = {status.provider for status in statuses}
                extras = [
                    _status_from_payload(payload)
                    for payload in _public_credential_payloads(org_id)
                    if payload['provider'] not in have
                ]
                return sorted([*statuses, *extras], key=lambda status: status.provider)
            except (ProgrammingError, SQLAlchemyError):
                return statuses

    def load_credential(self, org_id: str, provider: str) -> dict[str, str] | None:
        if db_session.SessionLocal is None:
            return None
        ciphertext = None
        try:
            with session_scope(commit=False, org_id=org_id) as db:
                row = _credential_query(db, org_id, provider).one_or_none()
                ciphertext = row.auth_key_ciphertext if row else None
        except TenantSchemaNotReady:
            ciphertext = None
        except ProgrammingError as exc:
            if not _is_missing_table(exc):
                raise
            ciphertext = None
        if ciphertext is None:
            for payload in _public_credential_payloads(org_id):
                if payload['provider'] == provider:
                    ciphertext = payload['auth_key_ciphertext']
                    break
        if ciphertext is None:
            return None
        return json.loads(decrypt_secret(ciphertext, context=credential_context(org_id, provider)))

    def save_credential(
        self,
        org_id: str,
        provider: str,
        fields: dict[str, str],
        *,
        hint: str,
        actor_id: str,
        provider_name: str = '',
        api_base: str | None = None,
    ) -> CredentialStatus:
        """Create or replace the org's key for ``provider``; enabled models are kept."""
        ciphertext = encrypt_secret(json.dumps(fields), context=credential_context(org_id, provider))
        now = datetime.utcnow()
        # Always keep a public leftover so Settings can see the key while the
        # tenant schema is catching up, or if another worker reads first.
        with platform_session_scope() as db:
            status = _upsert_credential_row(
                db, org_id, provider, ciphertext=ciphertext, hint=hint, actor_id=actor_id,
                provider_name=provider_name, api_base=api_base, now=now)
        tenant_ready = _tenant_schema_ready(org_id)
        if tenant_ready:
            with session_scope(org_id=org_id) as db:
                status = _upsert_credential_row(
                    db, org_id, provider, ciphertext=ciphertext, hint=hint, actor_id=actor_id,
                    provider_name=provider_name, api_base=api_base, now=now)
        logger.info('ai_credential_saved', org_id=org_id, provider=provider, actor_id=actor_id,
                    key_hint=hint, fields=sorted(fields), tenant=tenant_ready)
        return status

    def set_model_enabled(
        self, org_id: str, provider: str, model: str, *, enabled: bool, actor_id: str,
    ) -> CredentialStatus | None:
        """Toggle one model under an existing provider key. ``None`` when no key is stored."""
        now = datetime.utcnow()

        def _toggle(db) -> CredentialStatus | None:
            row = _credential_query(db, org_id, provider).one_or_none()
            if row is None:
                return None
            models = [m for m in (row.enabled_models or []) if m != model]
            if enabled:
                models.append(model)
            row.enabled_models = sorted(models)
            row.updated_by = actor_id
            row.updated_at = now
            db.flush()
            return _status(row)

        if _tenant_schema_ready(org_id):
            with session_scope(org_id=org_id) as db:
                status = _toggle(db)
            if status is None:
                with platform_session_scope() as db:
                    status = _toggle(db)
        else:
            with platform_session_scope() as db:
                status = _toggle(db)
        logger.info('ai_model_toggled', org_id=org_id, provider=provider, model=model,
                    enabled=enabled, actor_id=actor_id)
        return status

    def delete_credential(self, org_id: str, provider: str, *, actor_id: str) -> bool:
        with session_scope(org_id=org_id) as db:
            deleted = _credential_query(db, org_id, provider).delete(synchronize_session=False)
        logger.info('ai_credential_deleted', org_id=org_id, provider=provider, actor_id=actor_id, deleted=bool(deleted))
        return bool(deleted)
