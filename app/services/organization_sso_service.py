"""Organization enterprise SSO config, domain policy, and JIT membership."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.roles import ALL_ROLES, NOMINATED_TEAM_MEMBER, normalize_role
from app.models.core import OrganizationSsoConnection, OrganizationSsoDomain
from app.services.organization_membership_service import assign_role
from app.services.supabase_sso_admin import (
    SupabaseSsoAdminError,
    create_saml_provider,
    delete_saml_provider,
    saml_sp_info,
    update_saml_provider,
)

logger = structlog.get_logger(__name__)

_DOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().lstrip('@')
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


def extract_email_domain(email: str) -> str | None:
    trimmed = (email or '').strip().lower()
    if '@' not in trimmed:
        return None
    domain = normalize_domain(trimmed.rsplit('@', 1)[-1])
    return domain or None


def validate_domains(domains: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in domains:
        domain = normalize_domain(raw)
        if not domain or not _DOMAIN_RE.match(domain):
            raise ValueError(f'Invalid email domain: {raw}')
        if domain in seen:
            continue
        seen.add(domain)
        cleaned.append(domain)
    if not cleaned:
        raise ValueError('At least one email domain is required')
    return cleaned


def get_connection_for_org(db: Session, auth_org_id: str) -> OrganizationSsoConnection | None:
    return (
        db.query(OrganizationSsoConnection)
        .filter(OrganizationSsoConnection.auth_org_id == auth_org_id)
        .one_or_none()
    )


def list_domains_for_connection(db: Session, connection_id: str) -> list[str]:
    rows = (
        db.query(OrganizationSsoDomain)
        .filter(OrganizationSsoDomain.sso_connection_id == connection_id)
        .order_by(OrganizationSsoDomain.domain.asc())
        .all()
    )
    return [row.domain for row in rows]


def find_connection_by_domain(db: Session, domain: str) -> OrganizationSsoConnection | None:
    normalized = normalize_domain(domain)
    row = (
        db.query(OrganizationSsoDomain)
        .filter(OrganizationSsoDomain.domain == normalized)
        .one_or_none()
    )
    if row is None:
        return None
    return (
        db.query(OrganizationSsoConnection)
        .filter(OrganizationSsoConnection.id == row.sso_connection_id)
        .one_or_none()
    )


def login_policy_for_email(db: Session, email: str) -> dict[str, Any]:
    domain = extract_email_domain(email)
    if not domain:
        return {
            'domain': None,
            'sso_required': False,
            'sso_available': False,
            'auth_org_id': None,
        }
    connection = find_connection_by_domain(db, domain)
    if connection is None or connection.status == 'disabled':
        return {
            'domain': domain,
            'sso_required': False,
            'sso_available': False,
            'auth_org_id': None,
        }
    available = connection.status == 'active' and bool(connection.supabase_provider_id)
    return {
        'domain': domain,
        'sso_required': bool(connection.sso_only and available),
        'sso_available': available,
        'auth_org_id': connection.auth_org_id,
    }


def connection_payload(db: Session, connection: OrganizationSsoConnection, settings: Settings) -> dict[str, Any]:
    return {
        'id': connection.id,
        'auth_org_id': connection.auth_org_id,
        'domains': list_domains_for_connection(db, connection.id),
        'supabase_provider_id': connection.supabase_provider_id,
        'metadata_url': connection.metadata_url,
        'sso_only': connection.sso_only,
        'jit_enabled': connection.jit_enabled,
        'default_role': connection.default_role,
        'group_role_mappings': connection.group_role_mappings or {},
        'attribute_groups_key': connection.attribute_groups_key,
        'status': connection.status,
        'last_error': connection.last_error,
        'sp': saml_sp_info(settings) if settings.supabase_url else None,
        'updated_at': connection.updated_at.isoformat() if connection.updated_at else None,
    }


def _sync_domains(
    db: Session,
    *,
    connection: OrganizationSsoConnection,
    domains: list[str],
) -> None:
    existing = (
        db.query(OrganizationSsoDomain)
        .filter(OrganizationSsoDomain.sso_connection_id == connection.id)
        .all()
    )
    existing_by_domain = {row.domain: row for row in existing}
    desired = set(domains)

    for domain, row in list(existing_by_domain.items()):
        if domain not in desired:
            db.delete(row)

    for domain in domains:
        clash = (
            db.query(OrganizationSsoDomain)
            .filter(
                OrganizationSsoDomain.domain == domain,
                OrganizationSsoDomain.sso_connection_id != connection.id,
            )
            .one_or_none()
        )
        if clash is not None:
            raise ValueError(f'Domain "{domain}" is already linked to another organization')
        if domain not in existing_by_domain:
            db.add(
                OrganizationSsoDomain(
                    domain=domain,
                    auth_org_id=connection.auth_org_id,
                    sso_connection_id=connection.id,
                    created_at=_now(),
                )
            )


def upsert_org_sso_connection(
    db: Session,
    *,
    auth_org_id: str,
    domains: list[str],
    metadata_url: str | None,
    metadata_xml: str | None,
    sso_only: bool,
    jit_enabled: bool,
    default_role: str,
    group_role_mappings: dict[str, str],
    attribute_groups_key: str = 'groups',
    settings: Settings | None = None,
) -> OrganizationSsoConnection:
    settings = settings or get_settings()
    cleaned_domains = validate_domains(domains)
    role = normalize_role(default_role) or NOMINATED_TEAM_MEMBER
    if role not in ALL_ROLES:
        raise ValueError(f'Unsupported default role: {default_role}')

    mappings: dict[str, str] = {}
    for raw_group, raw_role in (group_role_mappings or {}).items():
        group = str(raw_group).strip()
        mapped = normalize_role(str(raw_role))
        if not group or not mapped or mapped not in ALL_ROLES:
            continue
        mappings[group] = mapped

    if not metadata_url and not metadata_xml:
        raise ValueError('Provide IdP metadata_url or metadata_xml')

    connection = get_connection_for_org(db, auth_org_id)
    if connection is None:
        connection = OrganizationSsoConnection(
            id=str(uuid4()),
            auth_org_id=auth_org_id,
            created_at=_now(),
        )
        db.add(connection)

    connection.metadata_url = (metadata_url or '').strip() or None
    connection.metadata_xml = (metadata_xml or '').strip() or None
    connection.sso_only = bool(sso_only)
    connection.jit_enabled = bool(jit_enabled)
    connection.default_role = role
    connection.group_role_mappings = mappings
    connection.attribute_groups_key = (attribute_groups_key or 'groups').strip() or 'groups'
    connection.updated_at = _now()
    connection.last_error = None

    _sync_domains(db, connection=connection, domains=cleaned_domains)
    db.flush()

    try:
        if connection.supabase_provider_id:
            provider = update_saml_provider(
                settings,
                connection.supabase_provider_id,
                domains=cleaned_domains,
                metadata_url=connection.metadata_url,
                metadata_xml=connection.metadata_xml,
                attribute_groups_key=connection.attribute_groups_key,
            )
        else:
            provider = create_saml_provider(
                settings,
                domains=cleaned_domains,
                metadata_url=connection.metadata_url,
                metadata_xml=connection.metadata_xml,
                attribute_groups_key=connection.attribute_groups_key,
            )
        connection.supabase_provider_id = str(provider['id'])
        connection.status = 'active'
        connection.last_error = None
    except SupabaseSsoAdminError as exc:
        # Persist config even when Supabase SAML is not enabled yet.
        connection.status = 'error' if connection.supabase_provider_id else 'pending'
        connection.last_error = str(exc)
        logger.warning(
            'org_sso_provider_sync_failed',
            auth_org_id=auth_org_id,
            error=str(exc),
            status_code=exc.status_code,
        )

    db.commit()
    db.refresh(connection)
    return connection


def delete_org_sso_connection(
    db: Session,
    *,
    auth_org_id: str,
    settings: Settings | None = None,
) -> bool:
    settings = settings or get_settings()
    connection = get_connection_for_org(db, auth_org_id)
    if connection is None:
        return False

    provider_id = connection.supabase_provider_id
    db.query(OrganizationSsoDomain).filter(
        OrganizationSsoDomain.sso_connection_id == connection.id
    ).delete(synchronize_session=False)
    db.delete(connection)
    db.commit()

    if provider_id:
        try:
            delete_saml_provider(settings, provider_id)
        except SupabaseSsoAdminError as exc:
            logger.warning(
                'org_sso_provider_delete_failed',
                auth_org_id=auth_org_id,
                provider_id=provider_id,
                error=str(exc),
            )
    return True


def _groups_from_claims(claims: dict[str, Any], attribute_key: str) -> list[str]:
    candidates: list[Any] = []
    app_metadata = claims.get('app_metadata') if isinstance(claims.get('app_metadata'), dict) else {}
    user_metadata = claims.get('user_metadata') if isinstance(claims.get('user_metadata'), dict) else {}
    amr = claims.get('amr')

    for source in (claims, app_metadata, user_metadata):
        if not isinstance(source, dict):
            continue
        if attribute_key in source:
            candidates.append(source.get(attribute_key))
        if 'groups' in source:
            candidates.append(source.get('groups'))
        custom = source.get('custom_claims')
        if isinstance(custom, dict) and attribute_key in custom:
            candidates.append(custom.get(attribute_key))

    if isinstance(amr, list):
        # not groups; ignore
        pass

    groups: list[str] = []
    for value in candidates:
        if isinstance(value, str) and value.strip():
            groups.append(value.strip())
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item.strip():
                    groups.append(item.strip())
    # Preserve order, unique
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        if group not in seen:
            seen.add(group)
            ordered.append(group)
    return ordered


def resolve_jit_role(connection: OrganizationSsoConnection, groups: list[str]) -> str:
    mappings = connection.group_role_mappings or {}
    for group in groups:
        mapped = mappings.get(group)
        if mapped:
            role = normalize_role(mapped)
            if role in ALL_ROLES:
                return role
    return normalize_role(connection.default_role) or NOMINATED_TEAM_MEMBER


def jit_sync_membership(
    db: Session,
    *,
    auth_user_id: str,
    email: str | None,
    full_name: str | None,
    claims: dict[str, Any],
    explicit_groups: list[str] | None = None,
) -> dict[str, Any]:
    domain = extract_email_domain(email or '')
    if not domain:
        return {'provisioned': False, 'reason': 'no_email_domain'}

    connection = find_connection_by_domain(db, domain)
    if connection is None or not connection.jit_enabled or connection.status == 'disabled':
        return {'provisioned': False, 'reason': 'no_sso_connection'}

    groups = explicit_groups if explicit_groups is not None else _groups_from_claims(
        claims, connection.attribute_groups_key
    )
    role = resolve_jit_role(connection, groups)
    member = assign_role(
        db,
        auth_org_id=connection.auth_org_id,
        user_id=auth_user_id,
        role=role,
        email=email,
        full_name=full_name,
    )
    db.commit()
    return {
        'provisioned': True,
        'auth_org_id': connection.auth_org_id,
        'role': member.role,
        'groups': groups,
    }
