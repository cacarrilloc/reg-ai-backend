"""GDPR-oriented organization purge (control-plane + dedicated tenant project)."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import HTTPException, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.member_capabilities import DELETE_RESOURCES, member_can
from app.core.security import CurrentUser
from app.models.core import (
    AuditLog,
    BillingCustomer,
    BusinessChange,
    BusinessChangeEventDelivery,
    ComplianceHistory,
    ContextDocument,
    DocumentChunk,
    GapAnalysis,
    MarketingContent,
    MarketingReview,
    Organization,
    OrganizationEntitlement,
    OrganizationInvite,
    OrganizationMember,
    OrganizationRolePermission,
    OrganizationTenant,
    Policy,
    PolicyRevision,
    Regulation,
    Subscription,
    SupportTicket,
    UsageCounter,
)
from app.services.internal_org_service import (
    RESERVED_INTERNAL_AUTH_ORG_IDS,
    is_internal_organization,
    platform_supabase_project_ref,
)
from app.services.organization_tenant_service import get_organization_tenant

logger = structlog.get_logger(__name__)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def purge_organization_data(db: Session, *, auth_org_id: str) -> dict[str, int]:
    """Delete all rows owned by ``auth_org_id`` from the shared/control-plane database.

    Regulatory publications are platform-wide and are intentionally left intact.
    """
    deleted: dict[str, int] = {}

    def _count_delete(model, *filters) -> int:
        count = db.query(model).filter(*filters).delete(synchronize_session=False)
        deleted[model.__tablename__] = int(count or 0)
        return deleted[model.__tablename__]

    # Leaf / dependent tables first (FK-safe order).
    _count_delete(AuditLog, AuditLog.org_id == auth_org_id)
    _count_delete(ComplianceHistory, ComplianceHistory.org_id == auth_org_id)
    _count_delete(PolicyRevision, PolicyRevision.org_id == auth_org_id)
    _count_delete(GapAnalysis, GapAnalysis.org_id == auth_org_id)
    _count_delete(DocumentChunk, DocumentChunk.org_id == auth_org_id)
    _count_delete(ContextDocument, ContextDocument.org_id == auth_org_id)
    _count_delete(Regulation, Regulation.org_id == auth_org_id)
    _count_delete(Policy, Policy.org_id == auth_org_id)
    _count_delete(
        BusinessChangeEventDelivery,
        BusinessChangeEventDelivery.org_id == auth_org_id,
    )
    _count_delete(BusinessChange, BusinessChange.org_id == auth_org_id)
    _count_delete(MarketingReview, MarketingReview.org_id == auth_org_id)
    _count_delete(MarketingContent, MarketingContent.org_id == auth_org_id)
    _count_delete(SupportTicket, SupportTicket.org_id == auth_org_id)
    _count_delete(UsageCounter, UsageCounter.org_id == auth_org_id)
    _count_delete(
        OrganizationEntitlement,
        OrganizationEntitlement.auth_org_id == auth_org_id,
    )
    _count_delete(Subscription, Subscription.auth_org_id == auth_org_id)
    _count_delete(BillingCustomer, BillingCustomer.auth_org_id == auth_org_id)
    _count_delete(OrganizationInvite, OrganizationInvite.auth_org_id == auth_org_id)
    _count_delete(
        OrganizationRolePermission,
        OrganizationRolePermission.auth_org_id == auth_org_id,
    )
    _count_delete(OrganizationMember, OrganizationMember.auth_org_id == auth_org_id)
    _count_delete(OrganizationTenant, OrganizationTenant.auth_org_id == auth_org_id)
    _count_delete(Organization, Organization.auth_org_id == auth_org_id)

    logger.info(
        'organization_purged',
        auth_org_id=auth_org_id,
        deleted=deleted,
        purged_at=_now().isoformat(),
    )
    return deleted


def resolve_dedicated_tenant_project_ref(
    db: Session,
    *,
    auth_org_id: str,
) -> str | None:
    """Return the dedicated Supabase project ref for an external org, if any.

    Never returns the platform project's ref (Development/Staging/Production DB).
    """
    settings = get_settings()
    tenant = get_organization_tenant(db, auth_org_id=auth_org_id)
    if tenant is None:
        return None
    project_ref = (tenant.supabase_project_ref or '').strip() or None
    if not project_ref:
        return None

    platform_ref = platform_supabase_project_ref(settings)
    if platform_ref and project_ref == platform_ref:
        logger.warning(
            'organization_delete_skipped_platform_project',
            auth_org_id=auth_org_id,
            project_ref=project_ref,
        )
        return None

    # Defensive: internal org ids must never destroy a shared project.
    if auth_org_id in RESERVED_INTERNAL_AUTH_ORG_IDS:
        return None

    return project_ref


def delete_current_organization(
    user: CurrentUser,
    db: Session | None,
) -> dict[str, object]:
    """Admin-only purge of the caller's active organization + tenant project."""
    if not member_can(user.role, DELETE_RESOURCES):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only Master and CCO users can delete resources',
        )
    if db is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='Database unavailable',
        )

    auth_org_id = (user.org_id or '').strip()
    if not auth_org_id or auth_org_id == 'platform':
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No active organization on the session',
        )

    organization = (
        db.query(Organization)
        .filter(Organization.auth_org_id == auth_org_id)
        .one_or_none()
    )
    if organization is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail='Organization not found',
        )

    if is_internal_organization(organization):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Internal Development/Staging/Production accounts cannot be deleted.',
        )

    org_name = organization.name
    tenant = get_organization_tenant(db, auth_org_id=auth_org_id)
    schema_name = (tenant.schema_name or '').strip() if tenant else ''
    isolation_mode = (tenant.isolation_mode or '').strip().lower() if tenant else ''
    schema_dropped = False
    if isolation_mode == 'schema' and schema_name and schema_name != 'public':
        from app.services.schema_tenant_provisioner import drop_organization_schema

        try:
            drop_organization_schema(schema_name)
            schema_dropped = True
        except Exception as exc:
            logger.exception(
                'organization_tenant_schema_delete_failed',
                auth_org_id=auth_org_id,
                schema_name=schema_name,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "Failed to drop the organization's database schema. "
                    'Organization data was not removed. Try again or contact support.'
                ),
            ) from exc

    project_ref = resolve_dedicated_tenant_project_ref(db, auth_org_id=auth_org_id)
    supabase_project_deleted = False
    supabase_project_ref: str | None = None

    if project_ref:
        from app.services.supabase_tenant_provisioner import delete_supabase_project

        try:
            delete_result = delete_supabase_project(project_ref=project_ref)
            supabase_project_deleted = bool(delete_result.get('deleted'))
            supabase_project_ref = project_ref
        except Exception as exc:
            logger.exception(
                'organization_tenant_project_delete_failed',
                auth_org_id=auth_org_id,
                project_ref=project_ref,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    'Failed to delete the organization\'s Supabase project. '
                    'Organization data was not removed. Try again or contact support.'
                ),
            ) from exc

    try:
        deleted = purge_organization_data(db, auth_org_id=auth_org_id)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception(
            'organization_purge_failed',
            auth_org_id=auth_org_id,
            error_type=type(exc).__name__,
            supabase_project_deleted=supabase_project_deleted,
            supabase_project_ref=supabase_project_ref,
        )
        detail = 'Failed to delete organization'
        if supabase_project_deleted:
            detail = (
                'The Supabase project was deleted, but clearing organization '
                'records failed. Contact support immediately.'
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=detail,
        ) from exc

    try:
        from app.db.session import clear_tenant_binding_cache

        clear_tenant_binding_cache(auth_org_id)
    except Exception:  # noqa: BLE001
        pass

    try:
        from app.services.organization_invite_service import clear_supabase_active_org

        clear_supabase_active_org(user.user_id, get_settings())
    except Exception:  # noqa: BLE001
        logger.warning(
            'organization_delete_clear_active_org_failed',
            auth_org_id=auth_org_id,
            actor_id=user.user_id,
        )

    logger.info(
        'organization_delete_requested',
        auth_org_id=auth_org_id,
        org_name=org_name,
        actor_id=user.user_id,
        deleted=deleted,
        supabase_project_deleted=supabase_project_deleted,
        supabase_project_ref=supabase_project_ref,
        schema_dropped=schema_dropped,
        schema_name=schema_name or None,
    )
    return {
        'auth_org_id': auth_org_id,
        'name': org_name,
        'deleted': True,
        'tables': deleted,
        'supabase_project_deleted': supabase_project_deleted,
        'supabase_project_ref': supabase_project_ref,
        'schema_dropped': schema_dropped,
        'schema_name': schema_name or None,
    }
