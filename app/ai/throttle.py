"""Rate limits for calls billed to Reg-AI's own Anthropic account (platform mode)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from functools import lru_cache

from app.ai.types import ThrottleTier
from app.core.config import Settings, get_settings
from app.services.rate_limiter import AsyncRateLimiter


class AsyncTokenBudgetLimiter:
    """Track estimated input tokens in a rolling 60-second window."""

    def __init__(self, tokens_per_minute: int) -> None:
        self.tokens_per_minute = max(tokens_per_minute, 1_000)
        self._usage: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._usage and now - self._usage[0][0] >= 60.0:
            self._usage.popleft()

    async def acquire(self, estimated_tokens: int) -> None:
        estimated_tokens = max(estimated_tokens, 1)
        async with self._lock:
            while True:
                now = time.monotonic()
                self._prune(now)
                used = sum(tokens for _, tokens in self._usage)
                if used + estimated_tokens <= self.tokens_per_minute or not self._usage:
                    self._usage.append((now, estimated_tokens))
                    return
                await asyncio.sleep(max(60.0 - (now - self._usage[0][0]) + 0.5, 1.0))


@lru_cache
def opus_limiter() -> AsyncRateLimiter:
    return AsyncRateLimiter(requests_per_second=1.0 / max(get_settings().anthropic_opus_min_interval_seconds, 1.0))


@lru_cache
def sonnet_limiter() -> AsyncRateLimiter:
    return AsyncRateLimiter(requests_per_second=1.0 / max(get_settings().anthropic_sonnet_min_interval_seconds, 0.5))


@lru_cache
def token_budget() -> AsyncTokenBudgetLimiter:
    return AsyncTokenBudgetLimiter(get_settings().anthropic_input_token_budget_per_minute)


def estimate_input_tokens(total_chars: int, settings: Settings) -> int:
    return max(256, total_chars // max(settings.anthropic_chars_per_token, 1) + 512)


async def throttle_platform_anthropic(
    tier: ThrottleTier, total_chars: int, settings: Settings, *, budget: bool = True
) -> None:
    await (opus_limiter() if tier == 'opus' else sonnet_limiter()).wait()
    if budget:
        await token_budget().acquire(estimate_input_tokens(total_chars, settings))
