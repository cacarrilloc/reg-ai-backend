"""Organization product entitlements (product_1 / product_2 feature flags)."""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.roles import PRODUCT_1, PRODUCT_2
from app.models.core import OrganizationEntitlement

DEFAULT_LAUNCH_PRODUCTS = (PRODUCT_1, PRODUCT_2)


def seed_default_entitlements(db: Session, *, auth_org_id: str) -> None:
    """Enable Product 1 + Product 2 for newly created orgs (launch default)."""
    for product_code in DEFAULT_LAUNCH_PRODUCTS:
        existing = (
            db.query(OrganizationEntitlement)
            .filter(
                OrganizationEntitlement.auth_org_id == auth_org_id,
                OrganizationEntitlement.product_code == product_code,
            )
            .one_or_none()
        )
        if existing is None:
            db.add(
                OrganizationEntitlement(
                    auth_org_id=auth_org_id,
                    product_code=product_code,
                    enabled=True,
                )
            )
        else:
            existing.enabled = True


def org_has_product(db: Session | None, *, auth_org_id: str, product_code: str) -> bool:
    """Return True when the org is entitled to the product.

    When DB is unavailable (local no-db mode), entitlement checks are skipped
    so Product 1 / Product 2 remain usable for development.

    Reserved internal Dev/Staging/Production orgs always have full product access.
    Customer (external) orgs continue to use organization_entitlements rows.
    """
    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    if is_reserved_internal_auth_org_id(auth_org_id):
        return True
    if db is None:
        return True
    row = (
        db.query(OrganizationEntitlement)
        .filter(
            OrganizationEntitlement.auth_org_id == auth_org_id,
            OrganizationEntitlement.product_code == product_code,
        )
        .one_or_none()
    )
    if row is None:
        # No row yet (pre-migration orgs): allow Product 1, deny Product 2 until seeded.
        return product_code == PRODUCT_1
    return bool(row.enabled)


def require_product(db: Session | None, *, auth_org_id: str, product_code: str) -> None:
    if not org_has_product(db, auth_org_id=auth_org_id, product_code=product_code):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f'Organization is not entitled to {product_code}',
        )
