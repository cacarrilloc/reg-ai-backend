from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.core.config import get_settings
from app.db.base import Base
from app.db.url_utils import sanitize_database_url
from app.models import core  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
target_metadata = Base.metadata


def database_url() -> str:
    """Resolve a migration DSN.

    Prefer ``DIRECT_URL`` (session / non-pooler) when set, then ``DATABASE_URL``.
    Always strip Prisma-only ``?pgbouncer=true`` which breaks psycopg/SQLAlchemy.
    """
    raw = getattr(settings, 'direct_url', None) or settings.database_url
    url = sanitize_database_url(raw)
    if not url:
        raise RuntimeError('DATABASE_URL (or DIRECT_URL) is required to run migrations')
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    config_section = config.get_section(config.config_ini_section, {})
    config_section['sqlalchemy.url'] = database_url()
    connectable = engine_from_config(config_section, prefix='sqlalchemy.', poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
