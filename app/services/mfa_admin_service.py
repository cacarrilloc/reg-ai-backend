"""Admin MFA factor management via Supabase Auth Admin API.

Client-side `mfa.unenroll` requires AAL2 for verified factors — a catch-22 when
the user cannot produce a valid TOTP (e.g. enrolled against a different
environment/authenticator entry). Password-authenticated users may request a
reset; the service role deletes factors so they can re-enroll with a new QR.
"""

from __future__ import annotations

import structlog

from app.core.config import Settings
from supabase import create_client

logger = structlog.get_logger(__name__)


class MfaAdminError(Exception):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


def reset_user_mfa_factors(*, settings: Settings, user_id: str) -> int:
    """Delete all MFA factors for ``user_id``. Returns the number removed."""
    url = (settings.supabase_url or "").strip().rstrip("/")
    key = (settings.supabase_service_role_key or "").strip()
    if not url or not key:
        raise MfaAdminError(
            "Supabase Admin is not configured for MFA reset",
            status_code=503,
        )
    if not user_id.strip():
        raise MfaAdminError("user_id is required", status_code=400)

    client = create_client(url, key)
    try:
        factors = client.auth.admin.mfa.list_factors({"user_id": user_id})
    except Exception as exc:  # noqa: BLE001 — surface Admin API failures
        logger.exception("mfa_list_factors_failed", user_id=user_id)
        raise MfaAdminError(
            f"Unable to list MFA factors: {exc}",
            status_code=502,
        ) from exc

    deleted = 0
    for factor in factors or []:
        factor_id = getattr(factor, "id", None) or (
            factor.get("id") if isinstance(factor, dict) else None
        )
        if not factor_id:
            continue
        try:
            client.auth.admin.mfa.delete_factor(
                {"user_id": user_id, "id": str(factor_id)}
            )
            deleted += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "mfa_delete_factor_failed",
                user_id=user_id,
                factor_id=factor_id,
            )
            raise MfaAdminError(
                f"Unable to delete MFA factor: {exc}",
                status_code=502,
            ) from exc

    logger.info("mfa_factors_reset", user_id=user_id, deleted=deleted)
    return deleted
