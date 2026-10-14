from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.core.member_capabilities import (
    DELETE_RESOURCES,
    MANAGE_NEWSLETTER_SUBSCRIBERS,
    capabilities_for_role,
    member_can,
    require_delete_resources,
)
from app.core.permissions import MANAGE_MEMBERS
from app.core.roles import (
    APPROVAL_CHAIN_MANAGER_ROLES,
    MASTER_USER,
    ORG_MANAGEMENT_ROLES,
    REVIEW_ACCESS_ROLES,
)
from app.core.security import CurrentUser, get_current_user
from app.db.session import get_platform_db
from app.schemas.organizations import (
    ApprovalChainUpdate,
    AssignedReviewerResponse,
    AssignedReviewerUpdate,
    CurrentOrganizationMembershipResponse,
    MyOrganizationsResponse,
    NominatedTeamMemberAssign,
    OrganizationAISetupCompleteRequest,
    OrganizationAISetupCompleteResponse,
    OrganizationApprovalChainResponse,
    OrganizationCreateRequest,
    OrganizationCreateResponse,
    OrganizationDeleteResponse,
    OrganizationInviteAcceptRequest,
    OrganizationInviteAcceptResponse,
    OrganizationInviteCancelResponse,
    OrganizationInviteCreate,
    OrganizationInviteListResponse,
    OrganizationInvitePreviewResponse,
    OrganizationInviteResponse,
    OrganizationLeaveResponse,
    OrganizationLogoResponse,
    OrganizationMemberListResponse,
    OrganizationMemberPermissionListResponse,
    OrganizationMemberPermissionResponse,
    OrganizationMemberPermissionsUpdate,
    OrganizationMemberRemoveResponse,
    OrganizationMemberRoleResponse,
    OrganizationMembersSyncResponse,
    OrganizationRolePermissionMatrixResponse,
    OrganizationRolePermissionsUpdate,
    OrganizationSettingsResponse,
    OrganizationSettingsUpdate,
    SwitchOrganizationRequest,
    SwitchOrganizationResponse,
)
from app.services.internal_org_service import is_reserved_internal_auth_org_id
from app.services.member_permissions_service import (
    list_member_permissions,
    reset_member_permissions,
    update_member_permissions,
)
from app.services.org_purge_service import delete_current_organization
from app.services.organization_invite_service import (
    accept_organization_invite,
    cancel_organization_invite,
    complete_organization_ai_setup,
    create_organization_for_user,
    create_organization_invite,
    list_my_organizations,
    list_organization_invites,
    preview_organization_invite,
    switch_active_organization,
    verify_organization_ai_key,
)
from app.services.organization_logo_service import (
    delete_organization_logo,
    upload_organization_logo,
)
from app.services.organization_service import (
    assign_nominated_team_member,
    get_approval_chain,
    get_organization_settings,
    leave_current_organization,
    list_organization_members,
    remove_current_organization_member,
    sync_current_organization_members,
    update_approval_chain,
    update_assigned_reviewer,
    update_organization_settings,
)
from app.services.role_permissions_service import (
    get_role_permission_matrix,
    update_role_permissions,
)

router = APIRouter(prefix='/organizations', tags=['organizations'])
INVITE_MANAGER_ROLES = ORG_MANAGEMENT_ROLES
# Review assignment / member list — same roles as the Review Queue.


def require_approval_chain_manager(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.has_any_role(APPROVAL_CHAIN_MANAGER_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to update the approval chain',
        )
    return user


def require_review_access(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.has_any_role(REVIEW_ACCESS_ROLES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to manage review assignments',
        )
    return user


def require_invite_manager(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if not user.has_any_role(INVITE_MANAGER_ROLES) and not user.has_permission(MANAGE_MEMBERS):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Insufficient role to manage organization invites',
        )
    return user


def require_member_permissions_viewer(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    if is_reserved_internal_auth_org_id(user.org_id):
        return user
    if MASTER_USER not in user.effective_roles() and normalize_role_safe(user.role) != MASTER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can view Member Permissions',
        )
    return user


def require_member_permissions_editor(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentUser:
    # External: Master only. Internal Dev/Staging/Production: any member can edit.
    if is_reserved_internal_auth_org_id(user.org_id):
        return user
    if MASTER_USER not in user.effective_roles() and normalize_role_safe(user.role) != MASTER_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master Users can manage Member Permissions',
        )
    return user


def normalize_role_safe(role: str | None) -> str:
    from app.core.roles import normalize_role

    return normalize_role(role)


@router.post('', response_model=OrganizationCreateResponse, status_code=status.HTTP_201_CREATED)
async def post_create_organization(
    payload: OrganizationCreateRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationCreateResponse:
    """A key entered here is tested with its provider first; no organization is created for a bad key."""
    await verify_organization_ai_key(payload.ai_setup)
    return await run_in_threadpool(create_organization_for_user, payload, user, db)


@router.post('/current/ai-setup', response_model=OrganizationAISetupCompleteResponse)
async def post_complete_organization_ai_setup(
    payload: OrganizationAISetupCompleteRequest,
    user: CurrentUser = Depends(get_current_user),
) -> OrganizationAISetupCompleteResponse:
    """Finish the registration AI choice after the org workspace exists."""
    await verify_organization_ai_key(payload.ai_setup)
    applied = await run_in_threadpool(complete_organization_ai_setup, payload.ai_setup, user)
    if not applied:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Organization workspace is still being isolated. Retry in a few seconds.',
        )
    return OrganizationAISetupCompleteResponse(ai_setup_applied=True)


@router.get('/mine', response_model=MyOrganizationsResponse)
def get_my_organizations(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> MyOrganizationsResponse:
    return list_my_organizations(user, db)


@router.post('/switch', response_model=SwitchOrganizationResponse)
def post_switch_active_organization(
    payload: SwitchOrganizationRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> SwitchOrganizationResponse:
    return switch_active_organization(payload, user, db)


@router.delete('/current', response_model=OrganizationDeleteResponse)
def delete_current_organization_endpoint(
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationDeleteResponse:
    """GDPR account/org deletion: purge the active organization's app data."""
    result = delete_current_organization(user, db)
    tables_raw = result.get('tables')
    tables: dict[str, int] = {}
    if isinstance(tables_raw, dict):
        tables = {str(key): int(value) for key, value in tables_raw.items()}
    return OrganizationDeleteResponse(
        auth_org_id=str(result['auth_org_id']),
        name=str(result['name']) if result.get('name') is not None else None,
        deleted=bool(result.get('deleted')),
        tables=tables,
        supabase_project_deleted=bool(result.get('supabase_project_deleted')),
        supabase_project_ref=(
            str(result['supabase_project_ref'])
            if result.get('supabase_project_ref') is not None
            else None
        ),
    )


@router.post(
    '/current/team-requests',
    response_model=OrganizationInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
def post_create_organization_invite(
    payload: OrganizationInviteCreate,
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteResponse:
    return create_organization_invite(payload, user, db)


@router.get('/current/team-requests', response_model=OrganizationInviteListResponse)
def get_organization_invites(
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteListResponse:
    return list_organization_invites(user, db)


@router.delete(
    '/current/team-requests/{invite_id}',
    response_model=OrganizationInviteCancelResponse,
)
def delete_organization_invite(
    invite_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteCancelResponse:
    """Delete a pending invitation. Master and CCO only."""
    return cancel_organization_invite(invite_id, user, db)


@router.get(
    '/team-requests/preview',
    response_model=OrganizationInvitePreviewResponse,
)
def get_organization_invite_preview(
    token: str,
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInvitePreviewResponse:
    """Unauthenticated invite preview for routing to sign-in/sign-up."""
    return preview_organization_invite(token, db)


@router.post('/team-requests/accept', response_model=OrganizationInviteAcceptResponse)
def post_accept_organization_invite(
    payload: OrganizationInviteAcceptRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteAcceptResponse:
    return accept_organization_invite(payload, user, db)


# Legacy aliases kept for older clients/emails. Prefer /team-requests*
# (many ad blockers drop paths containing "invit*").
@router.post(
    '/current/invitations',
    response_model=OrganizationInviteResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def post_create_organization_invite_invitations_alias(
    payload: OrganizationInviteCreate,
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteResponse:
    return create_organization_invite(payload, user, db)


@router.get(
    '/current/invitations',
    response_model=OrganizationInviteListResponse,
    include_in_schema=False,
)
def get_organization_invites_invitations_alias(
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteListResponse:
    return list_organization_invites(user, db)


@router.delete(
    '/current/invitations/{invite_id}',
    response_model=OrganizationInviteCancelResponse,
    include_in_schema=False,
)
def delete_organization_invite_invitations_alias(
    invite_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteCancelResponse:
    return cancel_organization_invite(invite_id, user, db)


@router.get(
    '/invitations/preview',
    response_model=OrganizationInvitePreviewResponse,
    include_in_schema=False,
)
def get_organization_invite_preview_invitations_alias(
    token: str,
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInvitePreviewResponse:
    return preview_organization_invite(token, db)


@router.post(
    '/invitations/accept',
    response_model=OrganizationInviteAcceptResponse,
    include_in_schema=False,
)
def post_accept_organization_invite_invitations_alias(
    payload: OrganizationInviteAcceptRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteAcceptResponse:
    return accept_organization_invite(payload, user, db)


@router.post(
    '/current/invites',
    response_model=OrganizationInviteResponse,
    status_code=status.HTTP_201_CREATED,
    include_in_schema=False,
)
def post_create_organization_invite_legacy(
    payload: OrganizationInviteCreate,
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteResponse:
    return create_organization_invite(payload, user, db)


@router.get(
    '/current/invites',
    response_model=OrganizationInviteListResponse,
    include_in_schema=False,
)
def get_organization_invites_legacy(
    user: CurrentUser = Depends(require_invite_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteListResponse:
    return list_organization_invites(user, db)


@router.delete(
    '/current/invites/{invite_id}',
    response_model=OrganizationInviteCancelResponse,
    include_in_schema=False,
)
def delete_organization_invite_legacy(
    invite_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteCancelResponse:
    return cancel_organization_invite(invite_id, user, db)


@router.get(
    '/invites/preview',
    response_model=OrganizationInvitePreviewResponse,
    include_in_schema=False,
)
def get_organization_invite_preview_legacy(
    token: str,
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInvitePreviewResponse:
    return preview_organization_invite(token, db)


@router.post(
    '/invites/accept',
    response_model=OrganizationInviteAcceptResponse,
    include_in_schema=False,
)
def post_accept_organization_invite_legacy(
    payload: OrganizationInviteAcceptRequest,
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationInviteAcceptResponse:
    return accept_organization_invite(payload, user, db)


@router.get('/current/settings', response_model=OrganizationSettingsResponse)
def get_current_organization_settings(
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
    x_org_name: str | None = Header(default=None, alias='x-org-name'),
) -> OrganizationSettingsResponse:
    return get_organization_settings(user, db, org_name=x_org_name)


@router.put('/current/settings', response_model=OrganizationSettingsResponse)
def put_current_organization_settings(
    payload: OrganizationSettingsUpdate,
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
    x_org_name: str | None = Header(default=None, alias='x-org-name'),
) -> OrganizationSettingsResponse:
    return update_organization_settings(payload, user, db, org_name=x_org_name)


@router.post('/current/logo', response_model=OrganizationLogoResponse)
async def post_current_organization_logo(
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationLogoResponse:
    result = await upload_organization_logo(file, user, db)
    return OrganizationLogoResponse(**result)


@router.delete('/current/logo', response_model=OrganizationLogoResponse)
def delete_current_organization_logo(
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationLogoResponse:
    result = delete_organization_logo(user, db)
    return OrganizationLogoResponse(**result)


@router.get('/current/approval-chain', response_model=OrganizationApprovalChainResponse)
def get_current_approval_chain(
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationApprovalChainResponse:
    return get_approval_chain(user, db)


@router.put('/current/approval-chain', response_model=OrganizationApprovalChainResponse)
def put_current_approval_chain(
    payload: ApprovalChainUpdate,
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationApprovalChainResponse:
    return update_approval_chain(payload, user, db)


@router.get('/current/members', response_model=OrganizationMemberListResponse)
def get_current_organization_members(
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberListResponse:
    return list_organization_members(user, db)


@router.delete(
    '/current/members/{user_id}',
    response_model=OrganizationMemberRemoveResponse,
)
def delete_current_organization_member(
    user_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberRemoveResponse:
    return remove_current_organization_member(user_id, user, db)


@router.post('/current/leave', response_model=OrganizationLeaveResponse)
def post_leave_current_organization(
    user: CurrentUser = Depends(get_current_user),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationLeaveResponse:
    return leave_current_organization(user, db)


@router.get('/current/me', response_model=CurrentOrganizationMembershipResponse)
def get_current_organization_membership(
    user: CurrentUser = Depends(get_current_user),
) -> CurrentOrganizationMembershipResponse:
    is_internal = is_reserved_internal_auth_org_id(user.org_id)
    is_master = (
        MASTER_USER in user.effective_roles() or normalize_role_safe(user.role) == MASTER_USER
    )
    can_view = is_internal or is_master
    can_edit = is_internal or is_master
    return CurrentOrganizationMembershipResponse(
        auth_org_id=user.org_id,
        user_id=user.user_id,
        role=normalize_role_safe(user.role),
        permissions=sorted(user.permissions()),
        is_reserved_internal=is_internal,
        can_manage_member_permissions=can_view,
        can_edit_member_permissions=can_edit,
        can_manage_newsletter_subscribers=member_can(
            user.role, MANAGE_NEWSLETTER_SUBSCRIBERS
        ),
        can_delete_resources=member_can(user.role, DELETE_RESOURCES),
        capabilities=capabilities_for_role(user.role),
    )


@router.get(
    '/current/role-permissions',
    response_model=OrganizationRolePermissionMatrixResponse,
)
def get_current_role_permissions(
    user: CurrentUser = Depends(require_member_permissions_viewer),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationRolePermissionMatrixResponse:
    return get_role_permission_matrix(user, db)


@router.put(
    '/current/role-permissions',
    response_model=OrganizationRolePermissionMatrixResponse,
)
def put_current_role_permissions(
    payload: OrganizationRolePermissionsUpdate,
    user: CurrentUser = Depends(require_member_permissions_editor),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationRolePermissionMatrixResponse:
    return update_role_permissions(payload, user, db)


@router.get(
    '/current/member-permissions',
    response_model=OrganizationMemberPermissionListResponse,
)
def get_current_member_permissions(
    user: CurrentUser = Depends(require_member_permissions_viewer),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberPermissionListResponse:
    return list_member_permissions(user, db)


@router.put(
    '/current/member-permissions/{user_id}',
    response_model=OrganizationMemberPermissionResponse,
)
def put_current_member_permissions(
    user_id: str,
    payload: OrganizationMemberPermissionsUpdate,
    user: CurrentUser = Depends(require_member_permissions_editor),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberPermissionResponse:
    return update_member_permissions(user_id, payload, user, db)


@router.delete(
    '/current/member-permissions/{user_id}',
    response_model=OrganizationMemberPermissionResponse,
)
def delete_current_member_permissions(
    user_id: str,
    user: CurrentUser = Depends(require_delete_resources),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberPermissionResponse:
    return reset_member_permissions(user_id, user, db)


@router.post('/current/sync-members', response_model=OrganizationMembersSyncResponse)
def post_sync_current_organization_members(
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_platform_db),
    x_org_name: str | None = Header(default=None, alias='x-org-name'),
) -> OrganizationMembersSyncResponse:
    """Report local ``organization_members`` totals (IdP sync no longer applies)."""
    return sync_current_organization_members(user, db, org_name=x_org_name)


@router.put('/current/assigned-reviewer', response_model=AssignedReviewerResponse)
def put_current_assigned_reviewer(
    payload: AssignedReviewerUpdate,
    user: CurrentUser = Depends(require_review_access),
    db: Session | None = Depends(get_platform_db),
) -> AssignedReviewerResponse:
    return update_assigned_reviewer(payload, user, db)


@router.post(
    '/current/nominated-team-members',
    response_model=OrganizationMemberRoleResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_nominated_team_member(
    payload: NominatedTeamMemberAssign,
    user: CurrentUser = Depends(require_approval_chain_manager),
    db: Session | None = Depends(get_platform_db),
) -> OrganizationMemberRoleResponse:
    return assign_nominated_team_member(payload, user, db)
