"""Tests for Postgres URL sanitization used by Alembic / Settings."""

from types import SimpleNamespace

from app.db.session import _build_engine_for_url, session_scope_or_existing
from app.db.url_utils import sanitize_database_url


def test_sanitize_strips_pgbouncer_query_param() -> None:
    url = (
        'postgresql://user:pass@aws-0-us-east-2.pooler.supabase.com:6543/postgres'
        '?pgbouncer=true&sslmode=require'
    )
    cleaned = sanitize_database_url(url)
    assert cleaned is not None
    assert 'pgbouncer' not in cleaned
    assert cleaned.startswith('postgresql+psycopg://')
    assert 'sslmode=require' in cleaned


def test_sanitize_promotes_bare_postgresql_scheme() -> None:
    assert sanitize_database_url('postgresql://u:p@localhost:5432/db') == (
        'postgresql+psycopg://u:p@localhost:5432/db'
    )


def test_sanitize_none_and_blank() -> None:
    assert sanitize_database_url(None) is None
    assert sanitize_database_url('  ') is None


def _engine_settings() -> SimpleNamespace:
    return SimpleNamespace(
        database_connect_timeout=10,
        database_pool_size=8,
        database_max_overflow=4,
        database_pool_timeout=30,
        database_pool_recycle=600,
    )


def test_session_mode_engine_skips_pre_ping_and_disables_jit(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url, **kwargs):
        captured['url'] = url
        captured.update(kwargs)
        return SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr('app.db.session.create_engine', fake_create_engine)
    _build_engine_for_url(
        'postgresql+psycopg://u:p@localhost:5432/postgres',
        _engine_settings(),
    )
    assert captured['pool_pre_ping'] is False
    assert captured['pool_use_lifo'] is True
    connect_args = captured['connect_args']
    assert isinstance(connect_args, dict)
    assert connect_args['options'] == '-c jit=off'
    assert 'prepare_threshold' not in connect_args


def test_transaction_pooler_engine_pre_pings_and_disables_prepares(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(dispose=lambda: None)

    monkeypatch.setattr('app.db.session.create_engine', fake_create_engine)
    _build_engine_for_url(
        'postgresql+psycopg://u:p@aws-0-us-east-2.pooler.supabase.com:6543/postgres',
        _engine_settings(),
    )
    assert captured['pool_pre_ping'] is True
    connect_args = captured['connect_args']
    assert isinstance(connect_args, dict)
    assert connect_args['prepare_threshold'] is None


def test_session_scope_or_existing_reuses_injected_session() -> None:
    injected = SimpleNamespace(closed=False)
    with session_scope_or_existing(injected, open_if_missing=False) as session:
        assert session is injected


def test_session_scope_or_existing_stays_none_when_asked_not_to_open() -> None:
    with session_scope_or_existing(None, open_if_missing=False) as session:
        assert session is None
