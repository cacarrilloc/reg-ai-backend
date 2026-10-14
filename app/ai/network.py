"""Validation for customer-supplied model endpoints (self-hosted / OpenAI-compatible)."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from app.ai.types import AIConfigurationError
from app.core.config import Settings, get_settings


def validate_api_base(url: str | None, settings: Settings | None = None) -> str | None:
    """Reject endpoints that would let a customer reach Reg-AI's private network.

    Requires https and a publicly routable host unless AI_ALLOW_PRIVATE_API_BASE
    is enabled (local development against a laptop Ollama, for example).
    Runs on save and again before every call.
    """
    if url is None or not url.strip():
        return None
    cfg = settings or get_settings()
    raw = url.strip().rstrip('/')
    parsed = urlparse(raw)
    allow_private = cfg.ai_allow_private_api_base and not cfg.is_production
    if parsed.scheme != 'https' and not (allow_private and parsed.scheme == 'http'):
        raise AIConfigurationError('Model endpoint must use https')
    if not parsed.hostname or parsed.username or parsed.password:
        raise AIConfigurationError('Model endpoint must be a plain https://host[:port]/path URL')
    if allow_private:
        return raw
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise AIConfigurationError('Model endpoint host could not be resolved') from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global or address.is_multicast:
            raise AIConfigurationError('Model endpoint must resolve to a public address')
    return raw
