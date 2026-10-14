"""Unit tests for encryption posture helpers."""

from app.core.config import Settings
from app.core.encryption_posture import database_url_requests_tls, encryption_posture


def test_database_url_requests_tls_supabase_host() -> None:
    assert (
        database_url_requests_tls(
            'postgresql://user:pass@db.abc.supabase.co:5432/postgres'
        )
        is True
    )


def test_database_url_requests_tls_explicit_sslmode() -> None:
    assert (
        database_url_requests_tls(
            'postgresql://user:pass@db.example.com:5432/postgres?sslmode=require'
        )
        is True
    )
    assert (
        database_url_requests_tls(
            'postgresql://user:pass@db.example.com:5432/postgres?sslmode=disable'
        )
        is False
    )


def test_encryption_posture_includes_mfa_flags() -> None:
    settings = Settings(
        ENVIRONMENT='production',
        DATABASE_URL='postgresql://user:pass@db.abc.supabase.co:5432/postgres',
        MFA_REQUIRED_FOR_ALL_USERS=True,
        MFA_REQUIRED_FOR_SENSITIVE_ACTIONS=True,
    )
    posture = encryption_posture(settings)
    assert posture['transport_https_expected'] is True
    assert posture['database_tls_requested'] is True
    assert posture['mfa_required_for_all_users'] is True
