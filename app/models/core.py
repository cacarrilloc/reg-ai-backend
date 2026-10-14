from __future__ import annotations

from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Computed

from app.db.base import Base


class Organization(Base):
    """Tenant profile backed by a Supabase Auth organization (master account)."""
    __tablename__ = 'organizations'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str | None] = mapped_column(String, unique=True, nullable=True)
    # internal = IPX env workspace (platform Supabase); external = customer tenant.
    kind: Mapped[str] = mapped_column(String, nullable=False, default='external')
    logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    regulatory_scope: Mapped[dict] = mapped_column(JSON, default=dict)
    cco_user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_chain: Mapped[dict] = mapped_column(JSON, default=dict)
    escalation_days: Mapped[int] = mapped_column(Integer, default=7)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationMember(Base):
    """Organization membership mirrored locally (sub-user under an org profile)."""

    __tablename__ = 'organization_members'
    __table_args__ = (
        UniqueConstraint('auth_org_id', 'auth_user_id', name='uq_org_members_org_user'),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    auth_user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String, nullable=False, default='chief_compliance_officer')
    # JSON array of granted System permission keys for this member.
    permissions_granted: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default='active')
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationInvite(Base):
    """Pending invitation for a user email to join an organization with a role."""

    __tablename__ = 'organization_invites'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    invitee_name: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String, nullable=False, default='nominated_team_member')
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationRolePermission(Base):
    """Per-org System role defaults: one row per role, nine permission columns."""

    __tablename__ = 'organization_role_permissions'

    auth_org_id: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, primary_key=True)
    manage_organization: Mapped[bool] = mapped_column(Boolean, default=False)
    delete_organization: Mapped[bool] = mapped_column(Boolean, default=False)
    read_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_billing: Mapped[bool] = mapped_column(Boolean, default=False)
    read_domains: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_domains: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_enterprise_connections: Mapped[bool] = mapped_column(Boolean, default=False)
    read_members: Mapped[bool] = mapped_column(Boolean, default=False)
    manage_members: Mapped[bool] = mapped_column(Boolean, default=False)
    total_enabled_permissions: Mapped[int] = mapped_column(
        Integer,
        Computed(
            '('
            '(CASE WHEN manage_organization THEN 1 ELSE 0 END)'
            ' + (CASE WHEN delete_organization THEN 1 ELSE 0 END)'
            ' + (CASE WHEN read_billing THEN 1 ELSE 0 END)'
            ' + (CASE WHEN manage_billing THEN 1 ELSE 0 END)'
            ' + (CASE WHEN read_domains THEN 1 ELSE 0 END)'
            ' + (CASE WHEN manage_domains THEN 1 ELSE 0 END)'
            ' + (CASE WHEN manage_enterprise_connections THEN 1 ELSE 0 END)'
            ' + (CASE WHEN read_members THEN 1 ELSE 0 END)'
            ' + (CASE WHEN manage_members THEN 1 ELSE 0 END)'
            ')'
        ),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationEntitlement(Base):
    """Per-org product feature flag (e.g. product_1, product_2)."""

    __tablename__ = 'organization_entitlements'

    auth_org_id: Mapped[str] = mapped_column(String, primary_key=True)
    product_code: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class BillingCustomer(Base):
    """Stripe customer mapping for an organization."""

    __tablename__ = 'billing_customers'

    auth_org_id: Mapped[str] = mapped_column(String, primary_key=True)
    stripe_customer_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class Subscription(Base):
    """Stripe subscription state mirrored locally for an organization."""

    __tablename__ = 'subscriptions'

    auth_org_id: Mapped[str] = mapped_column(String, primary_key=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    stripe_price_id: Mapped[str | None] = mapped_column(String, nullable=True)
    plan_code: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default='inactive')
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class MarketingContent(Base):
    """Uploaded marketing collateral pending or subject to compliance review."""

    __tablename__ = 'marketing_contents'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default='draft')
    uploaded_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    format: Mapped[str] = mapped_column(String, nullable=False, default='text')
    channel: Mapped[str | None] = mapped_column(String, nullable=True)
    audience: Mapped[str] = mapped_column(String, nullable=False, default='unknown')
    registrant_type: Mapped[str] = mapped_column(String, nullable=False, default='unknown')
    source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_tags: Mapped[list] = mapped_column(JSON, default=list)
    layout_notes: Mapped[list] = mapped_column(JSON, default=list)
    classification: Mapped[dict] = mapped_column(JSON, default=dict)
    published_locations: Mapped[list] = mapped_column(JSON, default=list)
    cadence_key: Mapped[str | None] = mapped_column(String, nullable=True)
    cadence_step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_content_type: Mapped[str | None] = mapped_column(String, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checksum_sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MarketingReview(Base):
    """Compliance review outcome for a piece of marketing content."""

    __tablename__ = 'marketing_reviews'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    content_id: Mapped[str] = mapped_column(
        String, ForeignKey('marketing_contents.id', ondelete='CASCADE'), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    findings: Mapped[list] = mapped_column(JSON, default=list)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    mode: Mapped[str] = mapped_column(String, nullable=False, default='A')
    trigger_type: Mapped[str | None] = mapped_column(String, nullable=True)
    trigger_id: Mapped[str | None] = mapped_column(String, nullable=True)
    publication_id: Mapped[str | None] = mapped_column(String, nullable=True)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    layout_notes: Mapped[list] = mapped_column(JSON, default=list)
    classification: Mapped[dict] = mapped_column(JSON, default=dict)
    rule_versions: Mapped[dict] = mapped_column(JSON, default=dict)
    change_list: Mapped[list] = mapped_column(JSON, default=list)
    selected_options: Mapped[list] = mapped_column(JSON, default=list)
    output_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    artifact_storage_path: Mapped[str | None] = mapped_column(String, nullable=True)
    skipped_archive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SupportTicket(Base):
    """Customer support request raised from the org workspace."""

    __tablename__ = 'support_tickets'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String, nullable=False)
    sender: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String, nullable=False, default='NORMAL')
    status: Mapped[str] = mapped_column(String, nullable=False, default='open')
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class UsageCounter(Base):
    """Daily per-org, per-metric usage counter for billing/limits."""

    __tablename__ = 'usage_counters'

    org_id: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[date] = mapped_column(primary_key=True)
    metric: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class UserProfile(Base):
    """App-level account profile: unique username bound 1:1 to an email/user."""

    __tablename__ = 'user_profiles'

    auth_user_id: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationSsoConnection(Base):
    """Self-serve enterprise SAML connection for an organization."""

    __tablename__ = 'organization_sso_connections'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    supabase_provider_id: Mapped[str | None] = mapped_column(String, nullable=True)
    metadata_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_xml: Mapped[str | None] = mapped_column(Text, nullable=True)
    sso_only: Mapped[bool] = mapped_column(Boolean, default=False)
    jit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    default_role: Mapped[str] = mapped_column(String, default='nominated_team_member')
    group_role_mappings: Mapped[dict] = mapped_column(JSON, default=dict)
    attribute_groups_key: Mapped[str] = mapped_column(String, default='groups')
    status: Mapped[str] = mapped_column(String, default='pending')
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationSsoDomain(Base):
    """Email domain routed to an organization SSO connection."""

    __tablename__ = 'organization_sso_domains'

    domain: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    sso_connection_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class OrganizationTenant(Base):
    """Control-plane registry: one data-plane binding per organization.

    Customer orgs get an isolated Postgres schema on the shared Supabase
    project. MASTER_ACCOUNT and internal orgs stay on the shared public schema.
    """

    __tablename__ = 'organization_tenants'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default='pending')
    isolation_mode: Mapped[str] = mapped_column(String, nullable=False, default='shared')
    schema_name: Mapped[str | None] = mapped_column(String, nullable=True)
    supabase_project_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    database_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    supabase_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    supabase_service_role_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    backend_url: Mapped[str | None] = mapped_column(String, nullable=True)
    frontend_url: Mapped[str | None] = mapped_column(String, nullable=True)
    provision_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class Policy(Base):
    __tablename__ = 'policies'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, default='uploaded')
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default='ACTIVE')
    file_url: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class Regulation(Base):
    """External / government regulations. Internal firm policies stay on ``policies``."""

    __tablename__ = 'regulations'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, default='REGULATORY_REFERENCE')
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String, default='ACTIVE')
    file_url: Mapped[str] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    rule_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class RegulatoryPublication(Base):
    __tablename__ = 'regulatory_publications'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    source: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=True)
    feed_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class ContextDocument(Base):
    __tablename__ = 'context_documents'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    document_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    file_url: Mapped[str] = mapped_column(String, nullable=True)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class DocumentChunk(Base):
    __tablename__ = 'document_chunks'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    policy_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('policies.id'), nullable=True)
    regulation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('regulations.id'), nullable=True)
    publication_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('regulatory_publications.id'), nullable=True
    )
    context_document_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('context_documents.id'), nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    policy_name: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(3072), nullable=True)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class BusinessChange(Base):
    __tablename__ = 'business_changes'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    change_type: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    flagged_by: Mapped[str] = mapped_column(String)
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    change_date: Mapped[date | None] = mapped_column(nullable=True)
    triggers_review: Mapped[bool] = mapped_column(Boolean, default=False)
    assessment_rationale: Mapped[str] = mapped_column(Text, default='')
    assessment_model: Mapped[str | None] = mapped_column(String, nullable=True)
    assessment_mode: Mapped[str] = mapped_column(
        String, default='RULE_FALLBACK')
    assessed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    event_summary: Mapped[str] = mapped_column(Text, default='')
    analysis_status: Mapped[str] = mapped_column(
        String, default='PENDING', nullable=False)
    analysis_file_url: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)


class BusinessChangeEventDelivery(Base):
    """Durable outbox entry for a flagged business-change workflow."""

    __tablename__ = 'business_change_event_deliveries'
    __table_args__ = (UniqueConstraint('business_change_id'),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    business_change_id: Mapped[str] = mapped_column(
        String, ForeignKey('business_changes.id', ondelete='CASCADE'), nullable=False
    )
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class GapAnalysis(Base):
    __tablename__ = 'gap_analyses'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    publication_id: Mapped[str] = mapped_column(
        String, ForeignKey('regulatory_publications.id'), nullable=True)
    business_change_id: Mapped[str] = mapped_column(
        String, ForeignKey('business_changes.id'), nullable=True)
    trigger_type: Mapped[str] = mapped_column(String, nullable=False)
    trigger_id: Mapped[str] = mapped_column(String, nullable=False)
    policy_id: Mapped[str] = mapped_column(String, ForeignKey('policies.id'))
    impact_score: Mapped[str] = mapped_column(String)
    gap_summary: Mapped[str] = mapped_column(Text)
    rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    fix: Mapped[str | None] = mapped_column(Text, nullable=True)
    regulatory_citations: Mapped[list] = mapped_column(JSON, default=list)
    retrieval_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_model: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_mode: Mapped[str] = mapped_column(String, default='RULE_FALLBACK')
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)


class ComplianceHistory(Base):
    """Denormalized historical record of gap incidents for the CCO dashboard."""

    __tablename__ = 'compliance_history'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    gap_analysis_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('gap_analyses.id', ondelete='SET NULL'), nullable=True
    )
    policy_id: Mapped[str | None] = mapped_column(
        String, ForeignKey('policies.id', ondelete='SET NULL'), nullable=True
    )
    policy_name: Mapped[str] = mapped_column(String, nullable=False)
    regulatory_rules: Mapped[list] = mapped_column(JSON, default=list)
    trigger_type: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    incident_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


class PolicyRevision(Base):
    __tablename__ = 'policy_revisions'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    gap_analysis_id: Mapped[str] = mapped_column(
        String, ForeignKey('gap_analyses.id'), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String, ForeignKey('policies.id'), nullable=False)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    redline_file_url: Mapped[str] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default='PENDING')
    reviewer_id: Mapped[str] = mapped_column(String, nullable=True)
    assigned_reviewer_name: Mapped[str | None] = mapped_column(String, nullable=True)
    assigned_reviewer_email: Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    approved_file_url: Mapped[str] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow)
    sla_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    review_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    reupload_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)
    reuploaded_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True)


class NewsletterSubscriber(Base):
    """Per-organization newsletter invite list (cloned onto tenant schemas)."""

    __tablename__ = 'newsletter_subscribers'
    __table_args__ = (
        UniqueConstraint('auth_org_id', 'email', name='newsletter_subscribers_org_email_key'),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    auth_org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default='invite')
    subscribed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class GeneralNewsletterSubscriber(Base):
    """Public newsletter signups. Lives only on the shared public schema."""

    __tablename__ = 'general_newsletter_subscribers'
    __table_args__ = (
        UniqueConstraint('email', name='general_newsletter_subscribers_email_key'),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False)
    subscribed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrganizationAISettings(Base):
    """Control-plane AI selection per org. Lives only on the shared public schema.

    No row means platform mode with the platform default models.
    """

    __tablename__ = 'organization_ai_settings'

    auth_org_id: Mapped[str] = mapped_column(String, primary_key=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, default='platform')
    # Redlines (tracked-change drafts): high volume, so cost and speed matter.
    generation_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_model: Mapped[str | None] = mapped_column(String, nullable=True)
    generation_api_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Analysis (gap analysis, compliance, business changes). Null uses the redline model.
    analysis_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_model: Mapped[str | None] = mapped_column(String, nullable=True)
    analysis_api_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fixed once saved: vectors from different models are not comparable.
    embedding_provider: Mapped[str] = mapped_column(String, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_api_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Native model width; vectors are zero-padded to the pgvector column width.
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class OrganizationAIReembedJob(Base):
    """Control plane: one embedding-model switch. At most one is running or swapped per org."""

    __tablename__ = 'organization_ai_reembed_jobs'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    auth_org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    # The AI settings saved when the new vectors are swapped in.
    target: Mapped[dict] = mapped_column(JSON, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, default=0)
    embedded_chunks: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    swapped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIReembedVector(Base):
    """Data plane: a chunk's vector from the new model, waiting to replace the current one."""

    __tablename__ = 'ai_reembed_vectors'

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(3072), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AIProviderCredential(Base):
    """One encrypted key per LLM company per org, shared by all of that company's models.

    Data plane: cloned onto each org tenant schema. Plaintext keys are never stored.
    """

    __tablename__ = 'ai_provider_credentials'
    __table_args__ = (
        UniqueConstraint('org_id', 'provider', name='ai_provider_credentials_org_provider_key'),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    # LiteLLM provider id (``openai``) and its display name (``OpenAI``).
    provider: Mapped[str] = mapped_column(String, nullable=False)
    provider_name: Mapped[str] = mapped_column(String, nullable=False)
    # AES-256-GCM bundle bound to ``org_id:provider``.
    auth_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    key_hint: Mapped[str] = mapped_column(String, nullable=False)
    api_base: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled_models: Mapped[list] = mapped_column(JSON, default=list)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = 'audit_logs'

    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String)
    actor_id: Mapped[str] = mapped_column(String)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True)
    revision_id: Mapped[str] = mapped_column(
        String, ForeignKey('policy_revisions.id'), nullable=True)
    policy_id: Mapped[str] = mapped_column(
        String, ForeignKey('policies.id'), nullable=True)
    gap_analysis_id: Mapped[str] = mapped_column(
        String, ForeignKey('gap_analyses.id'), nullable=True)
    metadata_json: Mapped[dict] = mapped_column('metadata', JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, nullable=True)
