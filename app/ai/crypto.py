"""AES-256-GCM encryption for customer API keys.

Ciphertext is bound to ``org_id:provider`` so a row copied to another org or
provider will not decrypt. The first configured key encrypts; every key
decrypts, which allows rotation.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.ai.types import AIEncryptionUnavailable
from app.core.config import Settings, get_settings

_VERSION = 'v1'


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def _keys(settings: Settings) -> dict[str, bytes]:
    keys: dict[str, bytes] = {}
    for raw in (settings.ai_credential_encryption_keys or '').split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            key = _b64decode(raw)
        except ValueError as exc:
            raise AIEncryptionUnavailable('AI credential encryption key is not valid base64') from exc
        if len(key) != 32:
            raise AIEncryptionUnavailable('AI credential encryption keys must be 32 bytes')
        keys[hashlib.sha256(key).hexdigest()[:12]] = key
    if not keys:
        raise AIEncryptionUnavailable('AI credential storage is not configured')
    return keys


def generate_encryption_key() -> str:
    """Return a new value for AI_CREDENTIAL_ENCRYPTION_KEYS."""
    return _b64encode(AESGCM.generate_key(bit_length=256))


def encrypt_secret(plaintext: str, *, context: str, settings: Settings | None = None) -> str:
    key_id, key = next(iter(_keys(settings or get_settings()).items()))
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode(), context.encode())
    return ':'.join((_VERSION, key_id, _b64encode(nonce), _b64encode(ciphertext)))


def decrypt_secret(token: str, *, context: str, settings: Settings | None = None) -> str:
    try:
        version, key_id, nonce, ciphertext = token.split(':')
    except ValueError as exc:
        raise AIEncryptionUnavailable('Stored AI credential is malformed') from exc
    key = _keys(settings or get_settings()).get(key_id)
    if version != _VERSION or key is None:
        raise AIEncryptionUnavailable('Stored AI credential uses an unknown encryption key')
    try:
        return AESGCM(key).decrypt(_b64decode(nonce), _b64decode(ciphertext), context.encode()).decode()
    except InvalidTag as exc:
        raise AIEncryptionUnavailable('Stored AI credential failed integrity check') from exc


def credential_context(org_id: str, provider: str) -> str:
    return f'{org_id}:{provider}'


def key_hint(secret: str) -> str:
    return f'…{secret[-4:]}' if len(secret) >= 8 else '…'
