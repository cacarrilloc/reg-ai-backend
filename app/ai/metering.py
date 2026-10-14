"""Usage metering for AI calls. Billing for "Use Reg-AI default models" will consume these events."""

from __future__ import annotations

from typing import Literal

import structlog

from app.ai.types import ResolvedModel

logger = structlog.get_logger(__name__)

BilledTo = Literal['reg_ai', 'customer']


def billed_to(target: ResolvedModel) -> BilledTo:
    """Reg-AI pays provider costs on platform keys; BYOK customers pay their providers."""
    return 'reg_ai' if target.source == 'platform' else 'customer'


def record_ai_usage(
    target: ResolvedModel,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    items: int = 0,
    trace_name: str,
) -> None:
    logger.info(
        'ai_usage',
        org_id=target.org_id,
        billed_to=billed_to(target),
        kind=target.kind,
        provider=target.provider,
        model=target.model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        items=items,
        trace_name=trace_name,
    )
