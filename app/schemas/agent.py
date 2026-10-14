from pydantic import BaseModel, Field


class AgentRunRequest(BaseModel):
    trigger_type: str = Field(
        ...,
        examples=['publication', 'business_change', 'policy_update'],
    )
    trigger_id: str
    org_id: str | None = None
    policy_ids: list[str] | None = None
    # Accepted for API compatibility; gap analysis always runs and overwrites.
    force_reanalysis: bool = False
    auto_generate_redlines: bool | None = None


class AgentRunResponse(BaseModel):
    run_id: str
    status: str
    message: str
