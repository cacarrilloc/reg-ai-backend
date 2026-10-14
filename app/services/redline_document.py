"""Helpers for locating policy text and applying surgical tracked changes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from docx import Document
from docx.document import Document as DocumentObject
from docx.text.paragraph import Paragraph


@dataclass(frozen=True)
class PolicyParagraph:
    index: int
    text: str
    heading: str | None = None


@dataclass(frozen=True)
class AnchorMatch:
    paragraph: Paragraph
    start: int
    end: int


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def iter_all_paragraphs(document: DocumentObject) -> list[Paragraph]:
    paragraphs = list(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                paragraphs.extend(cell.paragraphs)
    return paragraphs


def extract_policy_paragraphs(policy_bytes: bytes) -> list[PolicyParagraph]:
    document = Document(BytesIO(policy_bytes))
    current_heading: str | None = None
    extracted: list[PolicyParagraph] = []
    index = 0
    for paragraph in iter_all_paragraphs(document):
        text = paragraph.text.strip()
        if not text:
            continue
        style_name = str(getattr(paragraph.style, "name", "") or "")
        if style_name.startswith("Heading"):
            current_heading = text
            continue
        extracted.append(
            PolicyParagraph(index=index, text=text, heading=current_heading)
        )
        index += 1
    return extracted


def policy_text_from_paragraphs(paragraphs: list[PolicyParagraph]) -> str:
    return "\n".join(item.text for item in paragraphs)


def anchor_pattern(anchor: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in normalize_space(anchor).split() if part]
    if not parts:
        return re.compile(re.escape(anchor))
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def locate_anchor(document: DocumentObject, anchor: str) -> AnchorMatch | None:
    normalized_anchor = anchor.strip()
    if not normalized_anchor:
        return None

    pattern = anchor_pattern(normalized_anchor)
    for paragraph in iter_all_paragraphs(document):
        text = paragraph.text
        if not text.strip():
            continue
        exact_index = text.find(normalized_anchor)
        if exact_index >= 0:
            return AnchorMatch(
                paragraph=paragraph,
                start=exact_index,
                end=exact_index + len(normalized_anchor),
            )
        match = pattern.search(text)
        if match is not None:
            return AnchorMatch(paragraph=paragraph, start=match.start(), end=match.end())
    return None


def paragraph_index(document: DocumentObject, target: Paragraph) -> int:
    for index, paragraph in enumerate(iter_all_paragraphs(document)):
        if paragraph._p is target._p:
            return index
    return -1


def anchor_in_policy(anchor: str, policy_text: str) -> bool:
    normalized_anchor = normalize_space(anchor)
    if not normalized_anchor:
        return False
    normalized_policy = normalize_space(policy_text)
    if normalized_anchor in normalized_policy:
        return True
    pattern = anchor_pattern(anchor)
    return pattern.search(policy_text) is not None


def merge_replacement_with_anchor(anchor: str, replacement: str) -> str:
    """Prefer replacements that already embed the anchor's surrounding policy voice."""
    normalized_anchor = normalize_space(anchor)
    normalized_replacement = normalize_space(replacement)
    if not normalized_replacement:
        return anchor
    if normalized_anchor and normalized_anchor in normalized_replacement:
        return replacement.strip()
    if normalized_anchor and len(normalized_replacement) < len(normalized_anchor) * 0.5:
        return replacement.strip()
    return replacement.strip()


# Keep Applicable Regulation to compact rule-name + number labels only.
MAX_APPLICABLE_REGULATION_ITEM_CHARS = 64
MAX_APPLICABLE_REGULATION_TOTAL_CHARS = 800

_REGULATOR = r"(?:FINRA|SEC|CFTC|MSRB|NFA)"
_RULE_NUMBER = r"[\d.]+[()a-zA-Z0-9.\-]*"

# "FINRA Rule 3310", "FINRA Rules 3310 and 2090", "SEC Rule 17a-4"
_REGULATOR_RULES_PATTERN = re.compile(
    rf"(?ix)\b({_REGULATOR})\s+Rules?\s+"
    rf"({_RULE_NUMBER}(?:\s*(?:and|,|/|&)\s*{_RULE_NUMBER})*)"
)
# "FINRA 3310" / "SEC 17a-4" without an explicit "Rule" token
_REGULATOR_NUMBER_PATTERN = re.compile(
    rf"(?ix)\b({_REGULATOR})\s+({_RULE_NUMBER})\b"
)
# "31 CFR § 1023.320" / "31 CFR 1023.320"
_CFR_PATTERN = re.compile(
    r"(?ix)\b(\d{1,2}\s*CFR)\s*(?:§\s*)?([\d.]+[()a-zA-Z0-9.\-]*)"
)


def _regulator_display(name: str) -> str:
    return name.strip().upper()


def _split_rule_numbers(blob: str) -> list[str]:
    return [
        normalize_space(part)
        for part in re.split(r"\s*(?:and|,|/|&)\s*", blob or "")
        if normalize_space(part)
    ]


def canonicalize_regulation_citation(item: str) -> str | None:
    """Return a compact 'Regulator Rule <number>' (or CFR §) label, else None."""
    normalized = normalize_space(item)
    if not normalized or normalized.upper() == "TBD":
        return None
    if len(normalized) > MAX_APPLICABLE_REGULATION_ITEM_CHARS:
        return None

    match = _REGULATOR_RULES_PATTERN.fullmatch(normalized)
    if match:
        numbers = _split_rule_numbers(match.group(2))
        if len(numbers) == 1:
            return f"{_regulator_display(match.group(1))} Rule {numbers[0]}"
        return None

    match = _REGULATOR_NUMBER_PATTERN.fullmatch(normalized)
    if match:
        return f"{_regulator_display(match.group(1))} Rule {match.group(2)}"

    cfr = re.fullmatch(
        r"(?ix)(\d{1,2}\s*CFR)\s*§?\s*([\d.]+[()a-zA-Z0-9.\-]*)",
        normalized,
    )
    if cfr:
        return f"{normalize_space(cfr.group(1))} § {cfr.group(2)}"
    return None


def looks_like_regulation_citation(item: str) -> bool:
    """True when text is a compact rule-name + number label."""
    return canonicalize_regulation_citation(item) is not None


def extract_regulation_citations(text: str) -> list[str]:
    """Pull only rule-name + number labels out of RULE comments or free-form text.

    Examples kept: ``FINRA Rule 3310``, ``SEC Rule 17a-4``, ``31 CFR § 1023.320``.
    Prose, act names without numbers, and long obligation sentences are dropped.
    """
    normalized = normalize_space(text)
    if not normalized:
        return []

    found: list[str] = []
    seen: set[str] = set()

    def _add(label: str | None) -> None:
        if not label:
            return
        key = label.lower()
        if key in seen:
            return
        seen.add(key)
        found.append(label)

    # Prefer explicit "Regulator Rule(s) <numbers>" and expand conjunctions.
    for match in _REGULATOR_RULES_PATTERN.finditer(normalized):
        regulator = _regulator_display(match.group(1))
        for number in _split_rule_numbers(match.group(2)):
            _add(f"{regulator} Rule {number}")

    # "FINRA 3310" without the Rule token.
    for match in _REGULATOR_NUMBER_PATTERN.finditer(normalized):
        _add(f"{_regulator_display(match.group(1))} Rule {match.group(2)}")

    for match in _CFR_PATTERN.finditer(normalized):
        _add(f"{normalize_space(match.group(1))} § {match.group(2)}")

    if not found:
        _add(canonicalize_regulation_citation(normalized))

    return found


def merge_applicable_regulation(existing: str, *additional: str) -> str:
    """Merge rule-name + number citations without duplicating or keeping prose."""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(item: str) -> None:
        for citation in extract_regulation_citations(item):
            canonical = canonicalize_regulation_citation(citation) or citation
            key = canonical.lower()
            if key in seen:
                continue
            seen.add(key)
            parts.append(canonical)

    for block in (existing, *additional):
        text = normalize_space(block or "")
        if text:
            _add(text)

    if not parts:
        return "TBD"
    merged = "; ".join(parts)
    if len(merged) <= MAX_APPLICABLE_REGULATION_TOTAL_CHARS:
        return merged
    trimmed: list[str] = []
    used = 0
    for part in parts:
        extra = len(part) + (2 if trimmed else 0)
        if used + extra > MAX_APPLICABLE_REGULATION_TOTAL_CHARS:
            break
        trimmed.append(part)
        used += extra
    return "; ".join(trimmed) if trimmed else parts[0][:MAX_APPLICABLE_REGULATION_TOTAL_CHARS]


def labeled_metadata_lines(docx_bytes: bytes) -> dict[str, str]:
    """Return full metadata lines keyed by canonical label from a policy DOCX."""
    labels = (
        "Version",
        "Effective Date",
        "Applicable Regulation",
        "Next Review Date",
        "Approved Date",
    )
    approval_date_aliases = (
        "approved date",
        "approval date",
        "date approved",
        "date of approval",
        "approved/approval date",
    )
    lines: dict[str, str] = {}
    document = Document(BytesIO(docx_bytes))
    for paragraph in iter_all_paragraphs(document):
        text = normalize_space(paragraph.text)
        if not text:
            continue
        lowered = text.lower()
        for label in labels:
            if label == "Approved Date":
                continue
            prefix = f"{label}:"
            if lowered.startswith(prefix.lower()):
                lines[label] = text
        for alias in approval_date_aliases:
            if lowered.startswith(f"{alias}:"):
                lines["Approved Date"] = text
                break
    return lines


def labeled_metadata_value(docx_bytes: bytes, label: str) -> str:
    line = labeled_metadata_lines(docx_bytes).get(label, "")
    if not line:
        return ""
    if ":" not in line:
        return ""
    return line.split(":", 1)[1].strip()
