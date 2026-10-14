"""Unit tests for product entitlement helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.roles import PRODUCT_1, PRODUCT_2
from app.services.entitlement_service import (
    org_has_product,
    require_product,
    seed_default_entitlements,
)


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return self._row


class _Db:
    def __init__(self, row=None):
        self.row = row
        self.added = []

    def query(self, model):
        return _Query(self.row)

    def add(self, obj):
        self.added.append(obj)


def test_org_has_product_skips_check_without_db() -> None:
    assert org_has_product(None, auth_org_id='org_1', product_code=PRODUCT_2) is True


def test_org_has_product_defaults_product_1_when_missing() -> None:
    db = _Db(row=None)
    assert org_has_product(db, auth_org_id='org_1', product_code=PRODUCT_1) is True
    assert org_has_product(db, auth_org_id='org_1', product_code=PRODUCT_2) is False


def test_require_product_raises_when_disabled() -> None:
    db = _Db(row=SimpleNamespace(enabled=False))
    with pytest.raises(HTTPException) as exc:
        require_product(db, auth_org_id='org_1', product_code=PRODUCT_2)
    assert exc.value.status_code == 403


def test_seed_default_entitlements_adds_both_products() -> None:
    db = _Db(row=None)
    seed_default_entitlements(db, auth_org_id='org_new')
    codes = {item.product_code for item in db.added}
    assert codes == {PRODUCT_1, PRODUCT_2}
    assert all(item.enabled for item in db.added)


def test_org_has_product_always_true_for_reserved_internal_orgs() -> None:
    db = _Db(row=SimpleNamespace(enabled=False))
    assert org_has_product(
        db, auth_org_id='org_internal_development', product_code=PRODUCT_2
    ) is True
    assert org_has_product(
        db, auth_org_id='org_internal_staging', product_code=PRODUCT_1
    ) is True
    assert org_has_product(
        db, auth_org_id='org_internal_production', product_code=PRODUCT_2
    ) is True


def test_org_has_product_still_enforces_customer_org_entitlements() -> None:
    db = _Db(row=SimpleNamespace(enabled=False))
    assert org_has_product(db, auth_org_id='org_customer_1', product_code=PRODUCT_2) is False
