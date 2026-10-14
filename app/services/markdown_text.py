"""Strip Markdown markers before writing LLM text into Word documents."""

from __future__ import annotations

import re

EMPHASIS_PATTERN = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
ITALIC_PATTERN = re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)")
INLINE_CODE_PATTERN = re.compile(r"`([^`]+)`")
HEADING_PREFIX_PATTERN = re.compile(r"^#{1,6}\s+", re.MULTILINE)


def strip_markdown_emphasis(text: str) -> str:
    """Remove bold/italic/code markers while keeping the visible words."""
    cleaned = (text or "").replace("\r\n", "\n")
    cleaned = EMPHASIS_PATTERN.sub(
        lambda match: match.group(1) or match.group(2) or "",
        cleaned,
    )
    cleaned = ITALIC_PATTERN.sub(r"\1", cleaned)
    cleaned = INLINE_CODE_PATTERN.sub(r"\1", cleaned)
    cleaned = HEADING_PREFIX_PATTERN.sub("", cleaned)
    return cleaned.strip()
