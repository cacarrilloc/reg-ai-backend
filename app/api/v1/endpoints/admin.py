from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.roles import ADMIN_ORG_CREATE_ROLES
from app.core.security import CurrentUser, require_internal_role
from app.db.session import get_db
from app.schemas.admin import ClientOrganizationCreate, ClientOrganizationResponse
from app.services.admin_service import create_client_organization

router = APIRouter(prefix='/admin', tags=['admin'])


@router.post('/client-orgs', response_model=ClientOrganizationResponse, status_code=status.HTTP_201_CREATED)
def create_client_org(
    payload: ClientOrganizationCreate,
    user: CurrentUser = Depends(require_internal_role(*ADMIN_ORG_CREATE_ROLES)),
    db: Session | None = Depends(get_db),
) -> ClientOrganizationResponse:
    return create_client_organization(payload, user, db)
