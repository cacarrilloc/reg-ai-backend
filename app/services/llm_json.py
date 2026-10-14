"""Shared helpers for parsing JSON-only LLM responses."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from app.ai import GenerationResult

logger = structlog.get_logger(__name__)

LooseParser = Callable[[str], dict[str, Any] | None]
ModelT = TypeVar('ModelT', bound=BaseModel)


def response_text(response: GenerationResult | str) -> str:
    raw = response if isinstance(response, str) else response.text
    if isinstance(raw, str):
        return raw.strip()
    if raw is None:
        return ''
    return str(raw).strip()


def strip_markdown_json_fence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith('```'):
        return cleaned
    lines = cleaned.split('\n')
    if lines and lines[0].startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip() == '```':
        lines = lines[:-1]
    return '\n'.join(lines).strip()


def extract_balanced_json_object(text: str) -> str | None:
    start = text.find('{')
    if start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(text[start:], start):
        if in_string:
            if escape:
                escape = False
            elif char == '\\':
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def repair_json_text(text: str) -> str:
    repaired = (
        text.replace('\u201c', '"')
        .replace('\u201d', '"')
        .replace('\u2018', "'")
        .replace('\u2019', "'")
    )
    return re.sub(r',\s*([}\]])', r'\1', repaired)


def decode_json_string_fragment(raw: str) -> str:
    decoded: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        if char == '\\' and index + 1 < len(raw):
            next_char = raw[index + 1]
            if next_char == 'n':
                decoded.append('\n')
            elif next_char == 't':
                decoded.append('\t')
            elif next_char == 'r':
                decoded.append('\r')
            elif next_char in {'"', '\\', '/'}:
                decoded.append(next_char)
            else:
                decoded.append(next_char)
            index += 2
            continue
        decoded.append(char)
        index += 1
    return ''.join(decoded).strip()


def extract_json_string_field(text: str, keys: tuple[str, ...]) -> str:
    """Recover a string field when JSON is malformed or truncated."""
    for key in keys:
        marker = f'"{key}"'
        marker_index = text.find(marker)
        if marker_index < 0:
            continue

        remainder = text[marker_index + len(marker):].lstrip()
        if not remainder.startswith(':'):
            continue
        remainder = remainder[1:].lstrip()
        if not remainder.startswith('"'):
            continue

        raw_value = remainder[1:]
        end_index: int | None = None
        index = 0
        while index < len(raw_value):
            if raw_value[index] == '\\':
                index += 2
                continue
            if raw_value[index] == '"':
                tail = raw_value[index + 1:].lstrip()
                if tail.startswith(',') or tail.startswith('}'):
                    end_index = index
                    break
            index += 1

        fragment = raw_value[:end_index] if end_index is not None else raw_value
        fragment = fragment.rstrip('\\').strip()
        decoded = decode_json_string_fragment(fragment)
        if decoded:
            return decoded
    return ''


def parse_llm_json_dict(
    text: str,
    *,
    loose_parser: LooseParser | None = None,
    recovery_log_event: str = 'llm_json_recovered_with_loose_parser',
) -> dict[str, Any]:
    normalized = strip_markdown_json_fence(text)
    candidates = [normalized]
    extracted = extract_balanced_json_object(normalized)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    last_error: json.JSONDecodeError | None = None
    for candidate in candidates:
        for attempt_text in (candidate, repair_json_text(candidate)):
            try:
                data = json.loads(attempt_text)
            except json.JSONDecodeError as exc:
                last_error = exc
                continue
            if isinstance(data, dict):
                return data

    if loose_parser is not None:
        loose = loose_parser(text)
        if loose is not None:
            logger.info(
                recovery_log_event,
                truncated=extract_balanced_json_object(normalized) is None,
            )
            return loose

    if last_error is not None:
        raise last_error
    raise ValueError('Model response did not contain a JSON object.')


def truncate_text(value: str, max_length: int) -> str:
    normalized = value.strip()
    if len(normalized) <= max_length:
        return normalized
    if max_length <= 3:
        return normalized[:max_length]
    return normalized[: max_length - 3].rstrip() + '...'


def coerce_string_fields(data: dict[str, Any], field_limits: dict[str, int]) -> dict[str, Any]:
    coerced = dict(data)
    for field, max_length in field_limits.items():
        if field not in coerced or coerced[field] is None:
            continue
        text = str(coerced[field]).strip()
        coerced[field] = truncate_text(text, max_length) if text else ''
    return coerced


def response_model(
    response: GenerationResult | str,
    model_cls: type[ModelT],
    *,
    string_field_limits: dict[str, int] | None = None,
    loose_parser: LooseParser | None = None,
    recovery_log_event: str = 'llm_json_recovered_with_loose_parser',
) -> ModelT:
    """Parse an LLM response into a validated Pydantic model with safe string coercion."""
    raw_text = response_text(response)
    data = parse_llm_json_dict(
        raw_text,
        loose_parser=loose_parser,
        recovery_log_event=recovery_log_event,
    )
    if string_field_limits:
        data = coerce_string_fields(data, string_field_limits)

    try:
        return model_cls.model_validate(data)
    except ValidationError:
        if loose_parser is None:
            raise
        loose = loose_parser(raw_text)
        if loose is None:
            raise
        if string_field_limits:
            loose = coerce_string_fields(loose, string_field_limits)
        return model_cls.model_validate(loose)


def response_json(
    response: GenerationResult | str,
    *,
    loose_parser: LooseParser | None = None,
    recovery_log_event: str = 'llm_json_recovered_with_loose_parser',
) -> dict[str, Any]:
    data = parse_llm_json_dict(
        response_text(response),
        loose_parser=loose_parser,
        recovery_log_event=recovery_log_event,
    )
    if not isinstance(data, dict):
        raise ValueError('Model response is not a JSON object.')
    return data
