from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.core.security import CurrentUser, get_current_user, require_aal2
from app.db.session import get_db
from app.schemas.support import (
    SupportTicketCreate,
    SupportTicketListResponse,
    SupportTicketResponse,
    SupportTicketStatusUpdate,
)
from app.services.support_service import (
    create_support_ticket,
    list_support_tickets,
    update_support_ticket_status,
)

router = APIRouter(prefix='/support', tags=['support'])


@router.get('/tickets', response_model=SupportTicketListResponse)
def get_support_tickets(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_db),
) -> SupportTicketListResponse:
    return list_support_tickets(user, db)


@router.post('/tickets', response_model=SupportTicketResponse, status_code=status.HTTP_201_CREATED)
def post_support_ticket(
    payload: SupportTicketCreate,
    user: CurrentUser = Depends(require_aal2),
    db: Session | None = Depends(get_db),
) -> SupportTicketResponse:
    return create_support_ticket(payload, user, db)


@router.put('/tickets/{ticket_id}/status', response_model=SupportTicketResponse)
def put_support_ticket_status(
    ticket_id: str,
    payload: SupportTicketStatusUpdate,
    user: CurrentUser = Depends(require_aal2),
    db: Session | None = Depends(get_db),
) -> SupportTicketResponse:
    return update_support_ticket_status(ticket_id, payload, user, db)
