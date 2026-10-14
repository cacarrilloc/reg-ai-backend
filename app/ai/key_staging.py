"""Stage a registration key once, before the organization (and its schema) exist.

The user types the key only on the create-organization form. It is encrypted and
written to the shared ``public.ai_provider_credentials`` table under a per-user
pending id so any API worker can finish the transfer after the org exists.
A local file copy is kept as a same-process fallback.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from sqlalchemy.orm import sessionmaker

from app.ai.crypto import decrypt_secret, encrypt_secret, key_hint
from app.ai.settings_service import OwnKeyChoice
from app.ai.types import ModelKind
from app.core.config import get_settings

logger = structlog.get_logger(__name__)

_SAFE_USER = re.compile(r'[^a-zA-Z0-9._-]+')
_STAGING_TTL = timedelta(hours=1)
_STAGING_PROVIDER = '_registration'


def staging_org_id(user_id: str) -> str:
    return f'pending-user:{user_id}'


def db_backed() -> bool:
    from app.db import session as db_session

    return isinstance(db_session.SessionLocal, sessionmaker)


def staging_dir() -> Path:
    settings = get_settings()
    raw = (settings.ai_key_staging_dir or '').strip()
    path = Path(raw) if raw else Path(tempfile.gettempdir()) / 'reg-ai-ai-key-staging'
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _user_file(user_id: str) -> Path:
    safe = _SAFE_USER.sub('_', (user_id or '').strip())[:80] or 'anonymous'
    return staging_dir() / f'{safe}.enc'


def _context(user_id: str) -> str:
    return f'registration-staging:{user_id}'


def _payload(user_id: str, choice: OwnKeyChoice, dest_org_id: str | None = None) -> dict:
    return {
        'user_id': user_id,
        'provider': choice.provider,
        'model': choice.model,
        'kind': choice.kind,
        'fields': choice.fields,
        'api_base': choice.api_base,
        'dimensions': choice.dimensions,
        'dest_org_id': dest_org_id,
        'staged_at': datetime.now(UTC).replace(tzinfo=None).isoformat(),
    }


def _choice_from_payload(payload: dict, user_id: str) -> OwnKeyChoice | None:
    if payload.get('user_id') != user_id:
        return None
    staged_at = payload.get('staged_at')
    if staged_at:
        try:
            written = datetime.fromisoformat(str(staged_at))
            if datetime.now(UTC).replace(tzinfo=None) - written > _STAGING_TTL:
                logger.info('ai_key_staging_expired', user_id=user_id)
                return None
        except ValueError:
            pass
    kind: ModelKind = 'embedding' if payload.get('kind') == 'embedding' else 'generation'
    return OwnKeyChoice(
        provider=str(payload.get('provider') or ''),
        model=str(payload.get('model') or ''),
        kind=kind,
        fields=dict(payload.get('fields') or {}),
        api_base=payload.get('api_base'),
        dimensions=payload.get('dimensions'),
    )


def _encrypt_payload(user_id: str, payload: dict) -> str:
    return encrypt_secret(json.dumps(payload), context=_context(user_id))


def _decrypt_payload(user_id: str, ciphertext: str) -> dict | None:
    try:
        parsed = json.loads(decrypt_secret(ciphertext, context=_context(user_id)))
    except Exception:
        logger.warning('ai_key_staging_unreadable', user_id=user_id)
        return None
    return parsed if isinstance(parsed, dict) else None


def _write_file(user_id: str, ciphertext: str) -> Path:
    path = _user_file(user_id)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as handle:
        handle.write(ciphertext)
    return path


def _read_file_payload(user_id: str) -> dict | None:
    path = _user_file(user_id)
    if not path.is_file():
        return None
    return _decrypt_payload(user_id, path.read_text(encoding='utf-8').strip())


def _hint(choice: OwnKeyChoice) -> str:
    secret = next((value for value in choice.fields.values() if value), '')
    return key_hint(secret) if secret else '…'


def _upsert_db(user_id: str, payload: dict, choice: OwnKeyChoice) -> None:
    from app.db.session import platform_session_scope
    from app.models.core import AIProviderCredential

    now = datetime.utcnow()
    org_id = staging_org_id(user_id)
    ciphertext = _encrypt_payload(user_id, payload)
    with platform_session_scope() as db:
        row = (
            db.query(AIProviderCredential)
            .filter(
                AIProviderCredential.org_id == org_id,
                AIProviderCredential.provider == _STAGING_PROVIDER,
            )
            .one_or_none()
        )
        if row is None:
            row = AIProviderCredential(
                id=str(uuid.uuid4()),
                org_id=org_id,
                provider=_STAGING_PROVIDER,
                enabled_models=[],
                created_at=now,
            )
            db.add(row)
        row.provider_name = choice.provider
        row.auth_key_ciphertext = ciphertext
        row.key_hint = _hint(choice)
        row.api_base = choice.api_base
        row.enabled_models = [choice.model] if choice.model else []
        row.updated_by = user_id
        row.updated_at = now
        db.flush()
    logger.info('ai_key_staged_db', user_id=user_id, provider=choice.provider)


def _read_db_payload(user_id: str) -> dict | None:
    from app.db.session import platform_session_scope
    from app.models.core import AIProviderCredential

    org_id = staging_org_id(user_id)
    with platform_session_scope(commit=False) as db:
        row = (
            db.query(AIProviderCredential)
            .filter(
                AIProviderCredential.org_id == org_id,
                AIProviderCredential.provider == _STAGING_PROVIDER,
            )
            .one_or_none()
        )
        ciphertext = row.auth_key_ciphertext if row else None
    if not ciphertext:
        return None
    return _decrypt_payload(user_id, ciphertext)


def _delete_db(user_id: str) -> None:
    from app.db.session import platform_session_scope
    from app.models.core import AIProviderCredential

    org_id = staging_org_id(user_id)
    with platform_session_scope() as db:
        db.query(AIProviderCredential).filter(
            AIProviderCredential.org_id == org_id,
            AIProviderCredential.provider == _STAGING_PROVIDER,
        ).delete(synchronize_session=False)


def _load_payload(user_id: str) -> dict | None:
    if db_backed():
        try:
            payload = _read_db_payload(user_id)
        except Exception:
            logger.warning('ai_key_staging_db_read_failed', user_id=user_id)
            payload = None
        if payload is not None:
            return payload
    return _read_file_payload(user_id)


def stage_workflows(user_id: str, choices: list[OwnKeyChoice]) -> Path:
    """Persist one or two verified keys (analysis and/or redlines) before the org exists."""
    if not choices:
        raise ValueError('stage_workflows requires at least one choice')
    payload = _payload(user_id, choices[0])
    payload['workflows'] = [
        {
            'provider': choice.provider,
            'model': choice.model,
            'kind': choice.kind,
            'role': choice.role,
            'fields': choice.fields,
            'api_base': choice.api_base,
            'dimensions': choice.dimensions,
        }
        for choice in choices
    ]
    ciphertext = _encrypt_payload(user_id, payload)
    path = _write_file(user_id, ciphertext)
    if db_backed():
        _upsert_db(user_id, payload, choices[0])
    logger.info('ai_key_staged', user_id=user_id, provider=choices[0].provider, workflows=len(choices))
    return path


def _workflows_from_payload(payload: dict, user_id: str) -> list[OwnKeyChoice]:
    raw = payload.get('workflows')
    choices: list[OwnKeyChoice] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            kind: ModelKind = 'embedding' if item.get('kind') == 'embedding' else 'generation'
            role = item.get('role')
            choices.append(OwnKeyChoice(
                provider=str(item.get('provider') or ''),
                model=str(item.get('model') or ''),
                kind=kind,
                fields=dict(item.get('fields') or {}),
                api_base=item.get('api_base'),
                dimensions=item.get('dimensions'),
                role=str(role) if role in {'analysis', 'redline', 'embedding'} else None,
            ))
    return choices


def load_staged_workflows(user_id: str) -> list[OwnKeyChoice]:
    """Staged analysis and redline choices for this user."""
    payload = _load_payload(user_id)
    if payload is None:
        return []
    choices = _workflows_from_payload(payload, user_id)
    if not choices:
        discard_staged_key(user_id)
    return choices


def bind_staged_org(user_id: str, dest_org_id: str) -> None:
    """Remember which organization should receive the staged key."""
    payload = _load_payload(user_id)
    if payload is None:
        return
    payload['dest_org_id'] = dest_org_id
    choice = _choice_from_payload(payload, user_id)
    if choice is None:
        return
    ciphertext = _encrypt_payload(user_id, payload)
    _write_file(user_id, ciphertext)
    if db_backed():
        _upsert_db(user_id, payload, choice)


def staged_dest_org_id(user_id: str) -> str | None:
    payload = _load_payload(user_id)
    dest = (payload or {}).get('dest_org_id')
    return str(dest).strip() if dest else None


def discard_staged_key(user_id: str) -> None:
    path = _user_file(user_id)
    if path.is_file():
        path.unlink(missing_ok=True)
    if db_backed():
        try:
            _delete_db(user_id)
        except Exception:
            logger.warning('ai_key_staging_db_discard_failed', user_id=user_id)
            return
    logger.info('ai_key_staging_discarded', user_id=user_id)

