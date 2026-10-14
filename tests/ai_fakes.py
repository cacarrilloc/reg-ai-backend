"""Test doubles for the Reg-AI model layer (app.ai)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.ai import ChatMessage, GenerationResult


def result(text: str, *, stop_reason: str | None = 'stop', model: str = 'claude-test') -> GenerationResult:
    return GenerationResult(text=text, provider='anthropic', model=model, stop_reason=stop_reason)


class FakeLLM:
    """Records ``generate`` calls; replies are consumed in order (the last one repeats)."""

    def __init__(
        self,
        *replies: str | Exception | GenerationResult,
        model_label: str = 'claude-test',
        responder: Callable[[Sequence[ChatMessage], dict[str, Any]], str | GenerationResult] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.model_label = model_label
        self.responder = responder
        self.calls: list[dict[str, Any]] = []

    async def generate(self, messages: Sequence[ChatMessage], **kwargs: Any) -> GenerationResult:
        self.calls.append({'messages': list(messages), **kwargs})
        if self.responder is not None:
            reply: str | Exception | GenerationResult = self.responder(messages, kwargs)
        else:
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, GenerationResult) else result(reply, model=self.model_label)
