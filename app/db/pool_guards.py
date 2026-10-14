"""Limit concurrent background work that opens database connections."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from app.core.config import get_settings

_background_db_semaphore: asyncio.Semaphore | None = None


def _background_db_semaphore_limit() -> int:
    return max(1, get_settings().database_background_task_limit)


def _get_background_db_semaphore() -> asyncio.Semaphore:
    global _background_db_semaphore
    if _background_db_semaphore is None:
        _background_db_semaphore = asyncio.Semaphore(_background_db_semaphore_limit())
    return _background_db_semaphore


@asynccontextmanager
async def background_db_slot():
    """Serialize heavy background jobs so they cannot exhaust the SQLAlchemy pool."""
    semaphore = _get_background_db_semaphore()
    await semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()
