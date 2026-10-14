from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from threading import Lock
from typing import Any

from fastapi import Request
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings

settings = get_settings()

_tenant_org_id: ContextVar[str | None] = ContextVar('tenant_org_id', default=None)
_force_platform: ContextVar[bool] = ContextVar('force_platform', default=False)

_engine_cache: dict[str, Engine] = {}
_session_factory_cache: dict[str, sessionmaker[Session]] = {}
_engine_lock = Lock()
_tenant_binding_cache: dict[str, tuple[str, str | None, str | None, str | None] | None] = {}
_tenant_status_cache: dict[str, tuple[str, str] | None] = {}
_tenant_binding_lock = Lock()


def eager_load_columns(instance: Any) -> None:
    """Load scalar column values so ORM instances remain usable after detach."""
    for attr in instance.__mapper__.column_attrs:
        getattr(instance, attr.key)


def detach_instance(instance: Any, db: Session) -> Any:
    """Expunge an ORM instance after eagerly loading its column values."""
    eager_load_columns(instance)
    db.expunge(instance)
    return instance


def set_tenant_org_id(org_id: str | None) -> Token[str | None]:
    """Bind the active Clerk org for request/worker DB + storage routing."""
    normalized = (org_id or '').strip() or None
    if normalized in {'platform', 'dev-org'}:
        # platform is an internal sentinel; keep using the shared DB.
        if normalized == 'platform':
            normalized = None
    return _tenant_org_id.set(normalized)


def reset_tenant_org_id(token: Token[str | None]) -> None:
    _tenant_org_id.reset(token)


def get_tenant_org_id() -> str | None:
    return _tenant_org_id.get()


def clear_tenant_binding_cache(org_id: str | None = None) -> None:
    with _tenant_binding_lock:
        if org_id:
            key = org_id.strip()
            _tenant_binding_cache.pop(key, None)
            _tenant_status_cache.pop(key, None)
        else:
            _tenant_binding_cache.clear()
            _tenant_status_cache.clear()


def _uses_transaction_pooler(database_url: str) -> bool:
    return ':6543/' in database_url or ':6543?' in database_url


def _build_engine_for_url(database_url: str, cfg: Settings) -> Engine:
    connect_args: dict[str, object] = {
        'connect_timeout': cfg.database_connect_timeout,
        'keepalives': 1,
        'keepalives_idle': 30,
        'keepalives_interval': 10,
        'keepalives_count': 5,
        # Remote OLTP: planner JIT adds latency on short queries without helping.
        'options': '-c jit=off',
    }
    # Transaction-mode pgbouncer (6543) is more likely to hand out dead
    # connections; session mode (5432) keeps stickier sockets, so skip the
    # extra SELECT 1 round trip on every checkout (~50–100ms to remote Supabase).
    use_transaction_pooler = _uses_transaction_pooler(database_url)
    kwargs: dict[str, object] = {
        'pool_pre_ping': use_transaction_pooler,
        'pool_size': cfg.database_pool_size,
        'max_overflow': cfg.database_max_overflow,
        'pool_timeout': cfg.database_pool_timeout,
        'pool_recycle': cfg.database_pool_recycle,
        'pool_use_lifo': True,
        'pool_reset_on_return': 'rollback',
        'connect_args': connect_args,
    }
    if use_transaction_pooler:
        # pgbouncer transaction pooling reuses server connections across sessions,
        # so server-side prepared statements collide. None disables them entirely.
        connect_args['prepare_threshold'] = None
    return create_engine(database_url, **kwargs)


def dispose_engines() -> None:
    """Return pooled sockets on shutdown so Railway/Supabase do not sit on stale clients."""
    engines: list[Engine] = []
    if engine is not None:
        engines.append(engine)
    with _engine_lock:
        engines.extend(_engine_cache.values())
    for eng in engines:
        eng.dispose()


def _build_engine():
    if not settings.database_url:
        return None
    return _build_engine_for_url(settings.database_url, settings)


engine = _build_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None


def _session_factory_for_url(database_url: str) -> sessionmaker[Session]:
    with _engine_lock:
        cached = _session_factory_cache.get(database_url)
        if cached is not None:
            return cached
        eng = _engine_cache.get(database_url)
        if eng is None:
            eng = _build_engine_for_url(database_url, get_settings())
            _engine_cache[database_url] = eng
        factory = sessionmaker(autocommit=False, autoflush=False, bind=eng)
        _session_factory_cache[database_url] = factory
        return factory


def _lookup_tenant_status(org_id: str) -> tuple[str, str] | None:
    """Return (isolation_mode, status) from the control-plane registry."""
    org_id = org_id.strip()
    if not org_id:
        return None
    with _tenant_binding_lock:
        cached = _tenant_status_cache.get(org_id)
        # Never cache "no row yet": org creation looks up the id before the tenant
        # row commits, and a cached miss would skip isolation and write keys to public.
        if cached is not None:
            return cached
    if SessionLocal is None:
        return None

    from app.models.core import OrganizationTenant

    db = SessionLocal()
    try:
        row = (
            db.query(OrganizationTenant)
            .filter(OrganizationTenant.auth_org_id == org_id)
            .one_or_none()
        )
        status = None if row is None else (
            (row.isolation_mode or '').strip().lower() or 'shared',
            (row.status or '').strip().lower() or 'pending',
        )
    finally:
        db.close()
    from app.services.organization_tenant_service import TENANT_STATUS_READY

    # Only cache ready: a pending snapshot would keep dashboard requests
    # scheduling provision after the schema already exists.
    if status is not None and status[1] == TENANT_STATUS_READY:
        with _tenant_binding_lock:
            _tenant_status_cache[org_id] = status
    return status


class TenantSchemaNotReady(RuntimeError):
    """Raised when a schema-isolated org is not ready for data-plane queries."""


def prepare_org_data_plane(org_id: str | None) -> None:
    """Fail closed for schema tenants that are not ready.

    Internal orgs and MASTER_ACCOUNT-shared orgs stay on public. Customer orgs
    with ``isolation_mode=schema`` must not read or write the shared public
    data-plane tables while provisioning is pending or failed.
    """
    cfg = get_settings()
    if not cfg.tenant_db_routing_enabled or not org_id:
        return
    from app.services.internal_org_service import is_reserved_internal_auth_org_id
    from app.services.organization_tenant_service import (
        ISOLATION_SCHEMA,
        TENANT_STATUS_FAILED,
        TENANT_STATUS_READY,
    )

    if is_reserved_internal_auth_org_id(org_id):
        return
    state = _lookup_tenant_status(org_id)
    if state is None:
        return
    isolation_mode, status = state
    if isolation_mode != ISOLATION_SCHEMA:
        return
    if status == TENANT_STATUS_READY:
        return
    if status != TENANT_STATUS_FAILED:
        from app.services.tenant_provision_dispatch import (
            schedule_organization_tenant_provision,
        )

        clear_tenant_binding_cache(org_id)
        schedule_organization_tenant_provision(org_id)
        state = _lookup_tenant_status(org_id)
        if state is not None and state[1] == TENANT_STATUS_READY:
            return
        status = state[1] if state else status
    if status == TENANT_STATUS_FAILED:
        raise TenantSchemaNotReady(
            'Organization data isolation failed. Contact support.'
        )
    raise TenantSchemaNotReady(
        'Organization workspace is still being isolated. Retry in a few seconds.'
    )


def _lookup_ready_tenant_binding(
    org_id: str,
) -> tuple[str, str | None, str | None, str | None] | None:
    """Return (database_url, supabase_url, service_role_key, schema_name)."""
    org_id = org_id.strip()
    if not org_id:
        return None

    with _tenant_binding_lock:
        if org_id in _tenant_binding_cache:
            return _tenant_binding_cache[org_id]

    if SessionLocal is None:
        return None

    from app.models.core import OrganizationTenant
    from app.services.organization_tenant_service import (
        ISOLATION_SCHEMA,
        ISOLATION_SHARED,
        TENANT_STATUS_READY,
    )

    db = SessionLocal()
    try:
        row = (
            db.query(OrganizationTenant)
            .filter(OrganizationTenant.auth_org_id == org_id)
            .one_or_none()
        )
        if row is None:
            # Never cache a miss: org create looks up the id before the tenant
            # row commits. A cached None would keep uploads/gap analysis on
            # public after the isolated schema is ready.
            return None
        if row.status != TENANT_STATUS_READY:
            return None
        database_url = (row.database_url or '').strip()
        if not database_url:
            return None
        isolation = (row.isolation_mode or ISOLATION_SHARED).strip().lower()
        schema_name = (row.schema_name or '').strip() or None
        if isolation != ISOLATION_SCHEMA or schema_name in {None, 'public'}:
            schema_name = None
        binding = (
            database_url,
            (row.supabase_url or '').strip() or None,
            (row.supabase_service_role_key or '').strip() or None,
            schema_name,
        )
    finally:
        db.close()

    with _tenant_binding_lock:
        _tenant_binding_cache[org_id] = binding
    return binding


def resolve_database_url_for_org(
    org_id: str | None,
    *,
    force_platform: bool = False,
) -> str | None:
    """Platform DATABASE_URL, or tenant URL when routing is enabled and ready."""
    cfg = get_settings()
    platform_url = cfg.database_url
    if force_platform or _force_platform.get() or not cfg.tenant_db_routing_enabled:
        return platform_url
    if not org_id:
        return platform_url

    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    # Internal Dev/Stage/Prod orgs always use the platform project — skip the
    # organization_tenants lookup on every request.
    if is_reserved_internal_auth_org_id(org_id):
        return platform_url

    binding = _lookup_ready_tenant_binding(org_id)
    if binding is None:
        return platform_url
    return binding[0]


def resolve_storage_credentials_for_org(
    org_id: str | None,
) -> tuple[str | None, str | None]:
    """Return (supabase_url, service_role_key) for storage; platform defaults if unset."""
    cfg = get_settings()
    if not cfg.tenant_db_routing_enabled or not org_id:
        return cfg.supabase_url, cfg.supabase_service_role_key

    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    if is_reserved_internal_auth_org_id(org_id):
        return cfg.supabase_url, cfg.supabase_service_role_key

    binding = _lookup_ready_tenant_binding(org_id)
    if binding is None:
        return cfg.supabase_url, cfg.supabase_service_role_key
    _, supabase_url, service_role_key, _schema = binding
    return (
        supabase_url or cfg.supabase_url,
        service_role_key or cfg.supabase_service_role_key,
    )


def resolve_schema_for_org(
    org_id: str | None,
    *,
    force_platform: bool = False,
) -> str | None:
    """Return the dedicated tenant schema, or None for the shared public schema."""
    cfg = get_settings()
    if force_platform or _force_platform.get() or not cfg.tenant_db_routing_enabled:
        return None
    if not org_id:
        return None

    from app.services.internal_org_service import is_reserved_internal_auth_org_id

    if is_reserved_internal_auth_org_id(org_id):
        return None

    binding = _lookup_ready_tenant_binding(org_id)
    if binding is None:
        return None
    return binding[3]

def _session_factory(
    *,
    org_id: str | None = None,
    force_platform: bool = False,
) -> sessionmaker[Session]:
    if SessionLocal is None:
        raise RuntimeError('Database session requested but DATABASE_URL is not configured')
    effective_org = org_id if org_id is not None else get_tenant_org_id()
    database_url = resolve_database_url_for_org(
        effective_org, force_platform=force_platform
    )
    if not database_url:
        raise RuntimeError('Database session requested but DATABASE_URL is not configured')
    if database_url == settings.database_url:
        return SessionLocal
    return _session_factory_for_url(database_url)


def _bind_session_schema(db: Session, *, org_id: str | None, force_platform: bool) -> Session:
    schema = resolve_schema_for_org(org_id, force_platform=force_platform)
    if schema:
        db.info['tenant_schema'] = schema
    return db


def bind_data_plane_session(db: Session, *, org_id: str | None = None) -> Session:
    """Pin a session to the org tenant schema when isolation is enabled.

    No-ops for test doubles that lack SQLAlchemy's ``info`` dict, and for
    orgs that share ``public``.
    """
    info = getattr(db, 'info', None)
    if not isinstance(info, dict):
        return db
    effective = org_id if org_id is not None else get_tenant_org_id()
    return _bind_session_schema(db, org_id=effective, force_platform=False)


def use_shared_search_path(db: Session) -> None:
    """Point this transaction at ``public`` for shared feed tables.

    Tenant ``search_path`` is ``{org}_tenant, public``. Cloned
    ``document_chunks`` would otherwise hide global publication embeddings
    stored in ``public.document_chunks`` (org_id='global').
    """
    info = getattr(db, 'info', None)
    if not isinstance(info, dict) or not info.get('tenant_schema'):
        return
    execute = getattr(db, 'execute', None)
    if not callable(execute):
        return
    execute(text('SET LOCAL search_path TO public'))


@event.listens_for(Session, 'after_begin')
def _apply_tenant_search_path(session: Session, _transaction: Any, connection: Any) -> None:
    """Pin data-plane queries to the org schema for this transaction (pgbouncer-safe)."""
    schema = session.info.get('tenant_schema')
    if not schema or schema == 'public':
        return
    from app.services.schema_tenant_provisioner import TENANT_SCHEMA_RE, quote_pg_ident

    if not TENANT_SCHEMA_RE.fullmatch(str(schema)):
        return
    connection.execute(text(f'SET LOCAL search_path TO {quote_pg_ident(str(schema))}, public'))


@contextmanager
def platform_session_scope(*, commit: bool = True) -> Generator[Session, None, None]:
    """Always open a session against the shared platform database."""
    token = _force_platform.set(True)
    try:
        factory = _session_factory(force_platform=True)
        db = _bind_session_schema(factory(), org_id=None, force_platform=True)
        try:
            yield db
            if commit:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    finally:
        _force_platform.reset(token)


@contextmanager
def session_scope(
    *,
    commit: bool = True,
    org_id: str | None = None,
) -> Generator[Session, None, None]:
    """Open a short-lived database session and always return the connection to the pool."""
    effective_org = org_id if org_id is not None else get_tenant_org_id()
    prepare_org_data_plane(effective_org)
    factory = _session_factory(org_id=org_id)
    db = _bind_session_schema(factory(), org_id=effective_org, force_platform=False)
    try:
        yield db
        if commit:
            db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope_or_existing(
    db: Session | None,
    *,
    org_id: str | None = None,
    commit: bool = False,
    open_if_missing: bool = True,
) -> Generator[Session | None, None, None]:
    """Reuse an injected session, otherwise open a short-lived pooled session.

    Persist helpers that call ``commit()`` themselves should keep ``commit=False``.
    Set ``open_if_missing=False`` so ``db=None`` stays a no-database test path.
    """
    if db is not None:
        yield db
        return
    if not open_if_missing or SessionLocal is None:
        yield None
        return
    with session_scope(commit=commit, org_id=org_id) as session:
        yield session


async def get_db(request: Request) -> AsyncGenerator[Session | None, None]:
    """FastAPI dependency: route to tenant DB when the request org is ready.

    Async so setup/teardown share one context (sync generators run each half in
    a worker thread, which can leak pool checkouts and tenant ContextVars).
    """
    if SessionLocal is None:
        yield None
        return

    org_id = get_tenant_org_id()
    if not org_id:
        from app.core.security import CurrentUser

        state_user = getattr(request.state, 'current_user', None)
        if isinstance(state_user, CurrentUser):
            org_id = state_user.org_id

    try:
        prepare_org_data_plane(org_id)
    except TenantSchemaNotReady as exc:
        from fastapi import HTTPException
        from starlette import status as http_status

        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    try:
        factory = _session_factory(org_id=org_id)
    except RuntimeError:
        yield None
        return

    db = _bind_session_schema(factory(), org_id=org_id, force_platform=False)
    try:
        yield db
    finally:
        db.close()


async def get_platform_db() -> AsyncGenerator[Session | None, None]:
    """FastAPI dependency that always uses the platform DATABASE_URL.

    Declared async so setup and teardown share one context. A sync generator
    dependency runs each half in its own worker-thread context, which both
    discards the flag before the endpoint runs and makes the token invalid
    to reset.
    """
    if SessionLocal is None:
        yield None
        return
    token = _force_platform.set(True)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        _force_platform.reset(token)
