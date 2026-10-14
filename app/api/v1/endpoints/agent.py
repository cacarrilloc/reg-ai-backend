from fastapi import APIRouter, Depends, HTTPException, status

from app.core.roles import (
    CHIEF_COMPLIANCE_OFFICER,
    INTERNAL_ROLES,
    NOMINATED_TEAM_MEMBER,
)
from app.core.security import CurrentUser, get_current_user
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.services.agent_service import run_policy_change_agent

router = APIRouter(prefix='/agent', tags=['agent'])

AGENT_RUNNER_ROLES = INTERNAL_ROLES | {
    CHIEF_COMPLIANCE_OFFICER,
    NOMINATED_TEAM_MEMBER,
}


def require_agent_runner(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.has_any_role(AGENT_RUNNER_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to run a gap analysis',
        )
    return user


@router.post('/run', response_model=AgentRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_agent(
    payload: AgentRunRequest,
    user: CurrentUser = Depends(require_agent_runner),
) -> AgentRunResponse:
    if payload.org_id and payload.org_id != user.org_id and not user.has_any_role(INTERNAL_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Cannot run a gap analysis for another organization',
        )
    return await run_policy_change_agent(
        payload.model_copy(update={'org_id': payload.org_id or user.org_id})
    )
