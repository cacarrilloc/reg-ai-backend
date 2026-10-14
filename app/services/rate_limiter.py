from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    """Serialize async requests to not exceed a given rate."""

    def __init__(self, requests_per_second: float) -> None:
        self.minimum_interval = 1 / requests_per_second
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self.minimum_interval - (time.monotonic() - self._last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    async def penalize(self, seconds: float) -> None:
        """Push the next allowed request further out after a rate-limit response."""
        async with self._lock:
            self._last_request_at = max(
                self._last_request_at,
                time.monotonic() + max(seconds, 0.0),
            )
