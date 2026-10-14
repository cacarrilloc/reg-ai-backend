"""Bootstrap an internal admin onto each env's seeded internal organization.

Usage (credentials via env — never commit them):

  BOOTSTRAP_USERNAME=reg-ai-admin \\
  BOOTSTRAP_EMAIL=haider.mumtaz@ipxenterprises.com \\
  BOOTSTRAP_PASSWORD='...' \\
  .venv/bin/python scripts/bootstrap_internal_admin.py
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.roles import MASTER_USER  # noqa: E402
from app.db.url_utils import sanitize_database_url  # noqa: E402
from app.services.internal_org_service import (  # noqa: E402
    seed_internal_environment_organization,
)
from app.services.org_lifecycle_service import (  # noqa: E402
    MEMBER_STATUS_ACTIVE,
    upsert_member,
)
from app.services.user_profile_service import claim_user_profile  # noqa: E402


@dataclass(frozen=True)
class EnvTarget:
    label: str
    env_file: Path
    environment: str


TARGETS = (
    EnvTarget('local', BACKEND / 'deploy/.railway.env.local', 'local'),
    EnvTarget('staging', BACKEND / 'deploy/.railway.env.staging', 'staging'),
    EnvTarget('production', BACKEND / 'deploy/.railway.env.production', 'production'),
)


def env_get(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    match = re.search(rf'^{re.escape(key)}=(.*)$', path.read_text(), re.M)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def list_users_by_email(*, supabase_url: str, service_role: str, email: str) -> list[dict]:
    response = httpx.get(
        f'{supabase_url.rstrip("/")}/auth/v1/admin/users',
        headers={
            'apikey': service_role,
            'Authorization': f'Bearer {service_role}',
        },
        params={'page': 1, 'per_page': 200},
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json()
    users = payload.get('users') if isinstance(payload, dict) else payload
    if not isinstance(users, list):
        return []
    email_l = email.lower()
    return [
        user
        for user in users
        if isinstance(user, dict) and str(user.get('email') or '').lower() == email_l
    ]


def upsert_auth_user(
    *,
    supabase_url: str,
    service_role: str,
    email: str,
    password: str,
    username: str,
) -> dict:
    existing = list_users_by_email(
        supabase_url=supabase_url, service_role=service_role, email=email
    )
    headers = {
        'apikey': service_role,
        'Authorization': f'Bearer {service_role}',
        'Content-Type': 'application/json',
    }
    body = {
        'email': email,
        'password': password,
        'email_confirm': True,
        'user_metadata': {'username': username},
        'app_metadata': {},
    }
    if existing:
        user_id = str(existing[0]['id'])
        response = httpx.put(
            f'{supabase_url.rstrip("/")}/auth/v1/admin/users/{user_id}',
            headers=headers,
            json={
                'password': password,
                'email_confirm': True,
                'user_metadata': {'username': username},
            },
            timeout=30.0,
        )
        response.raise_for_status()
        user = response.json()
        print(f'  auth user updated id={user_id}')
        return user if isinstance(user, dict) else {'id': user_id, 'email': email}

    response = httpx.post(
        f'{supabase_url.rstrip("/")}/auth/v1/admin/users',
        headers=headers,
        json=body,
        timeout=30.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f'create user failed ({response.status_code}): {response.text[:400]}'
        )
    user = response.json()
    print(f'  auth user created id={user.get("id")}')
    return user


def set_active_org(
    *,
    supabase_url: str,
    service_role: str,
    user_id: str,
    auth_org_id: str,
    role: str,
) -> None:
    response = httpx.put(
        f'{supabase_url.rstrip("/")}/auth/v1/admin/users/{user_id}',
        headers={
            'apikey': service_role,
            'Authorization': f'Bearer {service_role}',
            'Content-Type': 'application/json',
        },
        json={
            'app_metadata': {
                'active_org_id': auth_org_id,
                'org_id': auth_org_id,
                'role': role,
            }
        },
        timeout=30.0,
    )
    response.raise_for_status()


def bootstrap_target(
    target: EnvTarget,
    *,
    username: str,
    email: str,
    password: str,
) -> None:
    print(f'=== {target.label} ({target.environment}) ===')
    supabase_url = env_get(target.env_file, 'SUPABASE_URL')
    service_role = env_get(target.env_file, 'SUPABASE_SERVICE_ROLE_KEY')
    # Prefer direct (non-pooler) URL for admin/migrations-style scripts.
    database_url = env_get(target.env_file, 'DIRECT_URL') or env_get(
        target.env_file, 'DATABASE_URL'
    )
    if not supabase_url or not service_role or not database_url:
        raise RuntimeError(f'{target.label}: missing SUPABASE_URL/SERVICE_ROLE_KEY/DATABASE_URL')

    sanitized = sanitize_database_url(database_url)
    if not sanitized:
        raise RuntimeError(f'{target.label}: DATABASE_URL/DIRECT_URL could not be sanitized')
    if 'pgbouncer' in sanitized.lower():
        raise RuntimeError(f'{target.label}: sanitized URL still contains pgbouncer')

    user = upsert_auth_user(
        supabase_url=supabase_url,
        service_role=service_role,
        email=email,
        password=password,
        username=username,
    )
    user_id = str(user.get('id') or '').strip()
    if not user_id:
        raise RuntimeError(f'{target.label}: auth user missing id')

    engine = create_engine(sanitized, pool_pre_ping=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        ENVIRONMENT=target.environment,
        SUPABASE_URL=supabase_url,
        SUPABASE_SERVICE_ROLE_KEY=service_role,
        DATABASE_URL=sanitized,
        FRONTEND_URL=env_get(target.env_file, 'FRONTEND_URL') or 'http://localhost:3000',
    )

    with SessionLocal() as db:
        org = seed_internal_environment_organization(db, settings=settings)
        if org is None:
            raise RuntimeError(f'{target.label}: failed to seed internal org')
        upsert_member(
            db,
            auth_org_id=org.auth_org_id,
            auth_user_id=user_id,
            role=MASTER_USER,
            email=email,
            full_name=username,
            status=MEMBER_STATUS_ACTIVE,
            org_name=org.name,
        )
        claim_user_profile(
            db,
            auth_user_id=user_id,
            username=username,
            email=email,
            settings=settings,
        )
        db.commit()
        print(
            f'  internal org={org.name} auth_org_id={org.auth_org_id} '
            f'membership=master_user'
        )
        auth_org_id = org.auth_org_id

    set_active_org(
        supabase_url=supabase_url,
        service_role=service_role,
        user_id=user_id,
        auth_org_id=auth_org_id,
        role=MASTER_USER,
    )
    print(f'  active org set → {auth_org_id}')
    print(f'  user_id={user_id}')


def main() -> None:
    username = (os.environ.get('BOOTSTRAP_USERNAME') or '').strip().lower()
    email = (os.environ.get('BOOTSTRAP_EMAIL') or '').strip().lower()
    password = os.environ.get('BOOTSTRAP_PASSWORD') or ''
    if not username or not email or not password:
        raise SystemExit(
            'Set BOOTSTRAP_USERNAME, BOOTSTRAP_EMAIL, and BOOTSTRAP_PASSWORD'
        )

    # Local .env may be newer than deploy/.railway.env.local for service role.
    local_dotenv = BACKEND / '.env'
    if local_dotenv.is_file():
        # Prefer root .env for local target secrets when present.
        TARGETS_LOCAL = (
            EnvTarget('local', local_dotenv, 'local'),
            TARGETS[1],
            TARGETS[2],
        )
    else:
        TARGETS_LOCAL = TARGETS

    for target in TARGETS_LOCAL:
        bootstrap_target(target, username=username, email=email, password=password)
    print('done')


if __name__ == '__main__':
    main()
