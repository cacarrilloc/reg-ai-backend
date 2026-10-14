from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.roles import BILLING_ROLES
from app.core.security import CurrentUser, require_aal2
from app.db.session import get_db
from app.schemas.billing import (
    BillingStatusResponse,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    PortalSessionRequest,
    PortalSessionResponse,
)
from app.services.billing_service import (
    create_checkout_session,
    create_portal_session,
    get_billing_status,
)

router = APIRouter(prefix='/billing', tags=['billing'])


def require_billing_manager(
    user: CurrentUser = Depends(require_aal2),
) -> CurrentUser:
    if not user.has_any_role(BILLING_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to manage billing',
        )
    return user


@router.get('/status', response_model=BillingStatusResponse)
def get_status(
    user: CurrentUser = Depends(require_billing_manager),
    db: Session | None = Depends(get_db),
) -> BillingStatusResponse:
    return get_billing_status(user, db)


@router.post('/checkout-session', response_model=CheckoutSessionResponse)
def post_checkout_session(
    payload: CheckoutSessionRequest,
    user: CurrentUser = Depends(require_billing_manager),
    db: Session | None = Depends(get_db),
) -> CheckoutSessionResponse:
    return create_checkout_session(payload, user, db)


@router.post('/portal-session', response_model=PortalSessionResponse)
def post_portal_session(
    payload: PortalSessionRequest,
    user: CurrentUser = Depends(require_billing_manager),
    db: Session | None = Depends(get_db),
) -> PortalSessionResponse:
    return create_portal_session(payload, user, db)
