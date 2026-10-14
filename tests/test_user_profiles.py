"""Username / user profile helpers."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.user_profile_service import (
    is_username_available,
    normalize_email,
    normalize_username,
    resolve_login_email,
    validate_username,
)


def test_normalize_username_and_email() -> None:
    assert normalize_username(' unique_username ') == 'unique_username'
    assert normalize_email('  Person@Example.COM ') == 'person@example.com'


def test_validate_username_accepts_valid() -> None:
    assert validate_username('abc') == 'abc'
    assert validate_username('user_01') == 'user_01'
    assert validate_username('reg-ai-admin') == 'reg-ai-admin'


def test_validate_username_rejects_invalid() -> None:
    with pytest.raises(HTTPException) as exc:
        validate_username('ab')
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException):
        validate_username('-bad')
    with pytest.raises(HTTPException):
        validate_username('bad-')
    with pytest.raises(HTTPException):
        validate_username('Bad.Name')


def test_is_username_available_queries_profile(monkeypatch) -> None:
    class Query:
        def filter(self, *args, **kwargs):
            del args, kwargs
            return self

        def one_or_none(self):
            return None

    class DB:
        def query(self, *args, **kwargs):
            del args, kwargs
            return Query()

    assert is_username_available(DB(), 'fresh_user') is True


def test_resolve_login_email_accepts_email() -> None:
    class DB:
        def query(self, *args, **kwargs):
            raise AssertionError('email identifiers must not query profiles')

    assert resolve_login_email(DB(), '  Person@Example.COM ') == 'person@example.com'


def test_resolve_login_email_looks_up_username() -> None:
    class Profile:
        email = 'Ada@Example.com'

    class Query:
        def filter(self, *args, **kwargs):
            del args, kwargs
            return self

        def one_or_none(self):
            return Profile()

    class DB:
        def query(self, *args, **kwargs):
            del args, kwargs
            return Query()

    assert resolve_login_email(DB(), 'Ada_User') == 'ada@example.com'


def test_resolve_login_email_unknown_username_is_generic() -> None:
    class Query:
        def filter(self, *args, **kwargs):
            del args, kwargs
            return self

        def one_or_none(self):
            return None

    class DB:
        def query(self, *args, **kwargs):
            del args, kwargs
            return Query()

    with pytest.raises(HTTPException) as exc:
        resolve_login_email(DB(), 'missing_user')
    assert exc.value.status_code == 404
    assert exc.value.detail == 'Invalid login credentials'
