"""Helpers for normalizing Postgres URLs for SQLAlchemy / Alembic / psycopg."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Prisma-style query options that are not valid libpq / psycopg DSN keys.
_DROP_QUERY_KEYS = frozenset({
    'pgbouncer',
    'connection_limit',
    'pool_timeout',
})


def sanitize_database_url(url: str | None) -> str | None:
    """Return a SQLAlchemy-safe Postgres URL.

    - Strips Prisma-only query params such as ``pgbouncer=true``
    - Prefers the ``postgresql+psycopg`` dialect when the scheme is bare
      ``postgresql://`` / ``postgres://``
    - Does not rewrite username/password encoding (Supabase pooler usernames
      contain dots like ``postgres.<project_ref>``)
    """
    if url is None:
        return None
    cleaned = url.strip().strip('"').strip("'")
    if not cleaned:
        return None

    for prefix in ('postgres://',):
        if cleaned.startswith(prefix):
            cleaned = 'postgresql://' + cleaned.removeprefix(prefix)

    u = urlsplit(cleaned)
    scheme = u.scheme or 'postgresql'
    if scheme in {'postgresql', 'postgres'}:
        scheme = 'postgresql+psycopg'
    elif scheme == 'postgresql+psycopg2':
        scheme = 'postgresql+psycopg'

    query_items = [
        (key, value)
        for key, value in parse_qsl(u.query, keep_blank_values=True)
        if key.lower() not in _DROP_QUERY_KEYS
    ]
    return urlunsplit((scheme, u.netloc, u.path, urlencode(query_items), u.fragment))
