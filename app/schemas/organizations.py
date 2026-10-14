from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.ai import OrganizationAISetup


class OrganizationStructureMember(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    title: str = Field(..., min_length=2, max_length=160)
    user_id: str | None = Field(default=None, max_length=128)
    department: str | None = Field(default=None, max_length=160)


class ApprovingAuthority(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    title: str = Field(..., min_length=2, max_length=160)
    user_id: str | None = Field(default=None, max_length=128)
    authority: str = Field(default='POLICY_APPROVAL', max_length=160)
    order: int = Field(default=1, ge=1, le=20)


class ApprovalChainUpdate(BaseModel):
    org_structure: list[OrganizationStructureMember] = Field(
        default_factory=list)
    approving_authorities: list[ApprovingAuthority] = Field(
        default_factory=list)
    escalation_days: int | None = Field(default=None, ge=1, le=90)
    # Operational contacts used by Review Queue, SLA reminders, and escalations.
    assigned_reviewer_user_id: str | None = Field(default=None, max_length=128)
    assigned_reviewer_name: str | None = Field(default=None, max_length=160)
    assigned_reviewer_email: str | None = Field(default=None, max_length=254)
    cco_user_id: str | None = Field(default=None, max_length=128)
    cco_name: str | None = Field(default=None, max_length=160)
    cco_email: str | None = Field(default=None, max_length=254)


class OrganizationApprovalChainResponse(BaseModel):
    id: str
    auth_org_id: str
    name: str
    approval_chain: ApprovalChainUpdate
    escalation_days: int
    # Platform SLA for Review Queue reminder emails (env SLA_REMINDER_DAYS).
    sla_reminder_days: int = 3
    updated_at: datetime | None = None
    persisted: bool


class RegulatoryScopeUpdate(BaseModel):
    """Org-scoped regulators and optional firm context for assessments."""

    sources: list[str] = Field(default_factory=list)
    jurisdictions: list[str] = Field(default_factory=lambda: ['US'])
    products: list[str] = Field(default_factory=list)


class OrganizationSettingsUpdate(BaseModel):
    """Client-editable organization configuration stored in ``organizations``."""

    name: str | None = Field(default=None, min_length=2, max_length=160)
    regulatory_scope: RegulatoryScopeUpdate | None = None
    escalation_days: int | None = Field(default=None, ge=1, le=90)
    cco_user_id: str | None = Field(default=None, max_length=128)
    cco_name: str | None = Field(default=None, max_length=160)
    cco_email: str | None = Field(default=None, max_length=254)


class OrganizationSettingsResponse(BaseModel):
    id: str
    auth_org_id: str
    name: str
    logo_url: str | None = None
    logo_display_url: str | None = None
    cco_user_id: str | None = None
    cco_name: str | None = None
    cco_email: str | None = None
    regulatory_scope: dict[str, Any]
    available_sources: list[str]
    escalation_days: int
    sla_reminder_days: int = 3
    approval_chain: ApprovalChainUpdate
    updated_at: datetime | None = None
    persisted: bool
    bootstrapped: bool = False


class NominatedTeamMemberAssign(BaseModel):
    user_id: str = Field(..., min_length=2, max_length=128)


class OrganizationMemberRoleResponse(BaseModel):
    auth_org_id: str
    user_id: str
    role: str
    assigned: bool


class OrganizationMemberResponse(BaseModel):
    user_id: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    role: str | None = None
    image_url: str | None = None


class OrganizationMemberListResponse(BaseModel):
    members: list[OrganizationMemberResponse]
    total: int
    current_user_id: str | None = None
    can_manage_members: bool = False


class OrganizationMemberRemoveResponse(BaseModel):
    auth_org_id: str
    user_id: str
    removed: bool


class OrganizationLeaveResponse(BaseModel):
    auth_org_id: str
    left: bool


class CurrentOrganizationMembershipResponse(BaseModel):
    auth_org_id: str
    user_id: str
    role: str
    permissions: list[str]
    is_reserved_internal: bool = False
    can_manage_member_permissions: bool = False
    can_edit_member_permissions: bool = False
    can_manage_newsletter_subscribers: bool = False
    can_delete_resources: bool = False
    capabilities: list[str] = Field(default_factory=list)


class RolePermissionMatrixEntry(BaseModel):
    role: str
    permissions: list[str]
    is_default: bool = True


class OrganizationRolePermissionMatrixResponse(BaseModel):
    auth_org_id: str
    roles: list[RolePermissionMatrixEntry]
    catalog_keys: list[str]


class OrganizationRolePermissionsUpdate(BaseModel):
    role: str = Field(..., min_length=2, max_length=64)
    permissions: list[str] = Field(default_factory=list)


class MemberPermissionEntry(BaseModel):
    user_id: str
    email: str | None = None
    full_name: str | None = None
    role: str
    permissions: list[str]
    role_default_permissions: list[str]
    is_custom: bool = False


class OrganizationMemberPermissionListResponse(BaseModel):
    auth_org_id: str
    members: list[MemberPermissionEntry]
    catalog_keys: list[str]


class OrganizationMemberPermissionsUpdate(BaseModel):
    permissions: list[str] = Field(default_factory=list)


class OrganizationMemberPermissionResponse(BaseModel):
    auth_org_id: str
    user_id: str
    role: str
    permissions: list[str]
    role_default_permissions: list[str]
    is_custom: bool = False


class OrganizationMembersSyncResponse(BaseModel):
    auth_org_id: str
    synced: int
    total: int


class AssignedReviewerUpdate(BaseModel):
    user_id: str = Field(..., min_length=2, max_length=128)
    email: str | None = Field(default=None, max_length=254)
    full_name: str | None = Field(default=None, max_length=160)


class AssignedReviewerResponse(BaseModel):
    user_id: str | None = None
    email: str | None = None
    full_name: str | None = None
    persisted: bool


class OrganizationCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    # Mandatory: no organization is created without an AI model choice.
    ai_setup: OrganizationAISetup


class OrganizationCreateResponse(BaseModel):
    id: str
    auth_org_id: str
    name: str
    slug: str | None = None
    kind: str = 'external'
    persisted: bool = True
    # False when the workspace was not ready to store the AI choice; finish it in AI settings.
    ai_setup_applied: bool = False


class OrganizationAISetupCompleteRequest(BaseModel):
    ai_setup: OrganizationAISetup


class OrganizationAISetupCompleteResponse(BaseModel):
    ai_setup_applied: bool


class MyOrganizationSummary(BaseModel):
    auth_org_id: str
    name: str
    role: str
    status: str
    kind: str = 'external'
    logo_url: str | None = None
    logo_display_url: str | None = None


class OrganizationLogoResponse(BaseModel):
    logo_url: str | None = None
    logo_display_url: str | None = None


class MyOrganizationsResponse(BaseModel):
    organizations: list[MyOrganizationSummary]


class SwitchOrganizationRequest(BaseModel):
    auth_org_id: str = Field(..., min_length=1, max_length=128)


class SwitchOrganizationResponse(BaseModel):
    auth_org_id: str
    updated: bool


class OrganizationInviteCreate(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    role: str = Field(default='nominated_team_member', max_length=64)
    invitee_name: str | None = Field(default=None, max_length=160)


class OrganizationInviteResponse(BaseModel):
    id: str
    auth_org_id: str
    email: str
    invitee_name: str | None = None
    role: str
    token: str | None = None
    expires_at: datetime
    status: str
    created_at: datetime


class OrganizationInviteListResponse(BaseModel):
    invites: list[OrganizationInviteResponse]
    can_cancel_invites: bool = False


class OrganizationInviteCancelResponse(BaseModel):
    id: str
    email: str
    cancelled: bool


class OrganizationInviteAcceptRequest(BaseModel):
    token: str = Field(..., min_length=8, max_length=256)


class OrganizationInviteAcceptResponse(BaseModel):
    auth_org_id: str
    role: str
    joined: bool

    @field_validator('role')
    @classmethod
    def _non_empty_role(cls, value: str) -> str:
        return value or 'nominated_team_member'


class OrganizationInvitePreviewResponse(BaseModel):
    email: str
    role: str
    organization_name: str | None = None
    status: str
    expired: bool = False


class OrganizationDeleteResponse(BaseModel):
    auth_org_id: str
    name: str | None = None
    deleted: bool
    tables: dict[str, int] = Field(default_factory=dict)
    supabase_project_deleted: bool = False
    supabase_project_ref: str | None = None
    schema_dropped: bool = False
    schema_name: str | None = None
