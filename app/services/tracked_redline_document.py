"""Apply LLM revision text to an internal policy DOCX with Word track changes."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from io import BytesIO

import structlog
from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from docx.text.run import Run
from docx_revisions import RevisionDocument, RevisionParagraph

from app.services.redline_document import (
    PolicyParagraph,
    anchor_in_policy,
    extract_policy_paragraphs,
    iter_all_paragraphs,
    labeled_metadata_lines,
    merge_replacement_with_anchor,
    normalize_space,
    paragraph_index,
)

logger = structlog.get_logger(__name__)

REDLINE_AUTHOR = "Reg AI"
REDLINE_AUTHOR_INITIALS = "RA"
MIN_PARAGRAPH_CHARS = 12
APPENDIX_MARKERS = (
    "revision summary",
    "regulatory sources cited",
    "regulatory oversight report",
    "approved by:",
    "vector similarity",
    "gap analysis summary",
)
METADATA_LABELS = (
    "Version",
    "Effective Date",
    "Applicable Regulation",
    "Next Review Date",
    "Approved Date",
)
APPROVAL_DATE_LABEL_PREFIXES = (
    "approved date:",
    "approval date:",
    "date approved:",
    "date of approval:",
    "approved/approval date:",
)
HEADER_MARKERS = (
    "policy number:",
    "version:",
    "effective date:",
    "next review date:",
    "owner:",
    "applicable regulation:",
    "company:",
    "department:",
    "approved by:",
    *APPROVAL_DATE_LABEL_PREFIXES,
)
ANALYSIS_PROSE_PHRASES = (
    "based on the gap analysis",
    "gap analysis between",
    "gap analysis:",
    "the following redline document",
    "the following policy has been updated",
    "several updates are required",
    "primary gaps identified",
    "key changes include",
    "reg ai draft",
    "for cco review",
)
POLICY_SECTION_HEADING = re.compile(r"^\d+(?:\.\d+)*\.?\s+[A-Za-z]")
COVER_PAGE_MARKERS = (
    "policy:",
    "regulatory oversight report",
)
MAX_COVER_LINE_CHARS = 120
REVISION_APPENDIX_HEADINGS = frozenset(
    {
        "revision summary",
        "regulatory sources cited",
    }
)


@dataclass(frozen=True)
class TrackedReplacement:
    search_text: str
    replace_text: str
    comment: str | None = None


@dataclass(frozen=True)
class ParagraphOverlay:
    original_text: str
    revised_text: str
    comment: str | None = None


def enable_track_revisions(document: DocumentObject) -> None:
    """Turn on Word review mode for the document."""
    settings = document.settings.element
    existing = settings.find(qn("w:trackRevisions"))
    if existing is not None:
        existing.set(qn("w:val"), "true")
        return
    track_revisions = OxmlElement("w:trackRevisions")
    track_revisions.set(qn("w:val"), "true")
    settings.append(track_revisions)


def _is_boilerplate_paragraph(text: str) -> bool:
    normalized = normalize_space(text).lower()
    if not normalized:
        return True
    if len(normalized) < MIN_PARAGRAPH_CHARS:
        return True
    for marker in APPENDIX_MARKERS:
        if normalized.startswith(marker):
            return True
    for marker in HEADER_MARKERS:
        if normalized.startswith(marker):
            return True
    for marker in COVER_PAGE_MARKERS:
        if normalized.startswith(marker):
            return True
    if re.fullmatch(r"\d+(\.\d+)*\s+.+", normalized) and len(normalized.split()) <= 6:
        return True
    return False


def _text_is_numbered_section_heading(text: str) -> bool:
    normalized = normalize_space(text)
    if not normalized:
        return False
    return bool(POLICY_SECTION_HEADING.match(normalized))


def _looks_like_cover_line(text: str) -> bool:
    normalized = normalize_space(text).lower()
    if any(normalized.startswith(marker) for marker in COVER_PAGE_MARKERS):
        return True
    if any(normalized.startswith(marker) for marker in HEADER_MARKERS):
        return True
    if len(normalized) <= MAX_COVER_LINE_CHARS and normalized.isupper():
        return True
    return False


def _document_has_enterprise_cover(document: DocumentObject) -> bool:
    for paragraph in iter_all_paragraphs(document):
        text = normalize_space(paragraph.text).lower()
        if not text:
            continue
        if any(text.startswith(marker) for marker in COVER_PAGE_MARKERS):
            return True
        if any(text.startswith(marker) for marker in HEADER_MARKERS):
            return True
    return False


def _starts_policy_body(
    text: str,
    style_name: str,
    *,
    has_enterprise_cover: bool,
) -> bool:
    if _text_is_numbered_section_heading(text):
        return True
    if style_name not in {"Heading 1", "Heading 2"}:
        return False
    if _looks_like_cover_line(text):
        return False
    if has_enterprise_cover:
        return False
    return True


def _paragraph_is_section_heading(text: str, style_name: str) -> bool:
    return _text_is_numbered_section_heading(text)


def _looks_like_analysis_prose(text: str) -> bool:
    lowered = normalize_space(text).lower()
    if not lowered:
        return False
    if len(lowered) > 400 and "gap analysis" in lowered:
        return True
    return any(phrase in lowered for phrase in ANALYSIS_PROSE_PHRASES)


def _is_metadata_overlay(overlay: ParagraphOverlay) -> bool:
    return is_cover_or_metadata_line(overlay.original_text)


def is_cover_or_metadata_line(text: str) -> bool:
    """True for cover-page / header metadata lines the LLM must not revise."""
    lowered = normalize_space(text).lower()
    if not lowered:
        return False
    for label in METADATA_LABELS:
        if lowered.startswith(f"{label.lower()}:"):
            return True
    for marker in HEADER_MARKERS:
        if lowered.startswith(marker):
            return True
    for marker in COVER_PAGE_MARKERS:
        if lowered.startswith(marker) or lowered == marker:
            return True
    for prefix in APPROVAL_DATE_LABEL_PREFIXES:
        if lowered.startswith(prefix):
            return True
    if lowered.startswith("approved by:"):
        return True
    return False


def _cover_body_boundary_index(document: DocumentObject) -> int:
    """Paragraph index where numbered policy sections begin.

    When the document has no numbered section headings, treat the whole
    document as body so sentence/section overlays are not dropped.
    """
    paragraphs = list(iter_all_paragraphs(document))
    for index, paragraph in enumerate(paragraphs):
        text = normalize_space(paragraph.text)
        if _text_is_numbered_section_heading(text):
            return index
    return 0


def _overlay_change_is_plausible(original_text: str, revised_text: str) -> bool:
    """Reject overlays that replace cover-page lines with full section prose."""
    original = normalize_space(original_text)
    revised = normalize_space(revised_text)
    if not original or not revised or original == revised:
        return True
    original_lower = original.lower()
    for marker in COVER_PAGE_MARKERS:
        if original_lower.startswith(marker):
            return False
    if len(original) <= MAX_COVER_LINE_CHARS and len(revised) > max(len(original) * 3, 240):
        return False
    return True


def collect_internal_body_paragraphs(document: DocumentObject) -> list[str]:
    """Collect editable body paragraph text from the uploaded internal policy."""
    texts: list[str] = []
    body_started = False
    has_enterprise_cover = _document_has_enterprise_cover(document)
    for paragraph in iter_all_paragraphs(document):
        text = normalize_space(paragraph.text)
        if not text:
            continue
        style_name = str(getattr(paragraph.style, "name", "") or "")
        if not body_started:
            if _starts_policy_body(
                text,
                style_name,
                has_enterprise_cover=has_enterprise_cover,
            ):
                body_started = True
            else:
                continue
            if style_name.startswith("Heading") or _text_is_numbered_section_heading(text):
                continue
        else:
            if style_name.startswith("Heading"):
                heading_lower = text.lower()
                if heading_lower in REVISION_APPENDIX_HEADINGS:
                    break
                continue
        if _is_boilerplate_paragraph(text):
            continue
        texts.append(text)
    return texts


def _find_paragraph_by_text(document: DocumentObject, target_text: str) -> Paragraph | None:
    normalized_target = normalize_space(target_text)
    if not normalized_target:
        return None
    for paragraph in iter_all_paragraphs(document):
        if normalize_space(paragraph.text) == normalized_target:
            return paragraph
    return None


def _find_paragraph_by_label(document: DocumentObject, label: str) -> Paragraph | None:
    prefix = f"{label}:"
    for paragraph in iter_all_paragraphs(document):
        if normalize_space(paragraph.text).lower().startswith(prefix.lower()):
            return paragraph
    return None


def _find_paragraph_for_overlay(document: DocumentObject, overlay: ParagraphOverlay) -> Paragraph | None:
    if _is_metadata_overlay(overlay):
        label = normalize_space(overlay.original_text).split(":", 1)[0]
        return _find_paragraph_by_label(document, label)
    return _find_paragraph_by_text(document, overlay.original_text)


def extract_revision_body_paragraphs(revision_bytes: bytes) -> list[str]:
    """Extract substantive policy paragraphs from the LLM revision DOCX."""
    extracted = extract_policy_paragraphs(revision_bytes)
    revision_texts: list[str] = []
    body_started = False
    for item in extracted:
        heading = normalize_space(item.heading or "")
        if heading.lower() in REVISION_APPENDIX_HEADINGS:
            break
        text = normalize_space(item.text)
        if not text:
            continue
        if not body_started:
            if _text_is_numbered_section_heading(text) or _text_is_numbered_section_heading(heading):
                body_started = True
            else:
                continue
        if _text_is_numbered_section_heading(text):
            continue
        if _is_boilerplate_paragraph(text):
            continue
        revision_texts.append(text)
    return revision_texts


def filter_policy_paragraphs_to_body(paragraphs: list[PolicyParagraph]) -> list[PolicyParagraph]:
    """Drop cover-page paragraphs that appear before the first numbered section."""
    filtered: list[PolicyParagraph] = []
    body_started = False
    has_enterprise_cover = any(
        _looks_like_cover_line(normalize_space(item.text))
        or _looks_like_cover_line(normalize_space(item.heading or ""))
        for item in paragraphs
    )
    for item in paragraphs:
        text = normalize_space(item.text)
        if not text:
            continue
        heading = normalize_space(item.heading or "")
        if heading.lower() in REVISION_APPENDIX_HEADINGS:
            break
        style_name = "Heading 1" if heading else "Normal"
        if not body_started:
            if _text_is_numbered_section_heading(text) or _text_is_numbered_section_heading(heading):
                body_started = True
            elif heading and _starts_policy_body(
                heading,
                style_name,
                has_enterprise_cover=has_enterprise_cover,
            ):
                body_started = True
            else:
                continue
        if _text_is_numbered_section_heading(text):
            continue
        if _is_boilerplate_paragraph(text):
            continue
        filtered.append(item)
    return filtered


def _split_section_body(body: str, paragraph_count: int) -> list[str]:
    body = normalize_space(body)
    if not body:
        return [""] * max(paragraph_count, 0)
    if paragraph_count <= 1:
        return [body]
    chunks = [normalize_space(chunk) for chunk in re.split(r"\n{2,}|\n", body) if normalize_space(chunk)]
    if len(chunks) == paragraph_count:
        return chunks
    if len(chunks) > paragraph_count:
        merged = chunks[: paragraph_count - 1]
        merged.append(" ".join(chunks[paragraph_count - 1 :]))
        return merged
    while len(chunks) < paragraph_count:
        chunks.append("")
    return chunks


def _group_paragraph_indexes_by_heading(paragraphs: list[PolicyParagraph]) -> list[list[int]]:
    groups: list[list[int]] = []
    current_heading: object | None = None
    for index, item in enumerate(paragraphs):
        heading_key = item.heading or f"__section_{index}"
        if heading_key != current_heading:
            groups.append([])
            current_heading = heading_key
        groups[-1].append(index)
    return groups


def overlays_from_draft_sections(
    internal_paragraphs: list[PolicyParagraph],
    draft: object,
) -> dict[int, str]:
    """Map internal paragraph indexes to revised text using LLM section payloads."""
    overlay_map: dict[int, str] = {}
    policy_sections = getattr(draft, "policy_sections", None) or []
    if not internal_paragraphs or not policy_sections:
        return overlay_map

    grouped_indexes = _group_paragraph_indexes_by_heading(internal_paragraphs)
    for section_index, paragraph_indexes in enumerate(grouped_indexes):
        if section_index >= len(policy_sections):
            break
        revised_section = policy_sections[section_index]
        revised_body = normalize_space(getattr(revised_section, "body", "") or "")
        if not revised_body:
            revised_body = normalize_space(
                " ".join(
                    normalize_space(getattr(subsection, "body", "") or "")
                    for subsection in getattr(revised_section, "subsections", []) or []
                    if normalize_space(getattr(subsection, "body", "") or "")
                )
            )
        if not revised_body:
            continue

        body_indexes = [
            para_idx
            for para_idx in paragraph_indexes
            if normalize_space(internal_paragraphs[para_idx].text)
            and not is_cover_or_metadata_line(internal_paragraphs[para_idx].text)
        ]
        single_paragraph_body = "\n" not in (getattr(revised_section, "body", "") or "")
        if body_indexes and (len(body_indexes) == 1 or single_paragraph_body):
            matched = False
            for para_idx in body_indexes:
                original = normalize_space(internal_paragraphs[para_idx].text)
                if original and (
                    original in revised_body
                    or revised_body.lower().startswith(original[:32].lower())
                ):
                    overlay_map[para_idx] = revised_body
                    matched = True
                    break
            if not matched and body_indexes:
                overlay_map[body_indexes[0]] = revised_body
            continue

        revised_chunks = _split_section_body(revised_body, len(paragraph_indexes))
        for para_idx, revised_text in zip(paragraph_indexes, revised_chunks, strict=False):
            if normalize_space(revised_text):
                overlay_map[para_idx] = revised_text
    return overlay_map


def overlays_from_draft_edits(
    body_paragraphs: list[str],
    draft: object,
) -> dict[int, tuple[str, str | None]]:
    """Apply explicit LLM edit pairs onto matching internal paragraphs.

    Returns paragraph index -> (revised_text, optional citation_comment).
    """
    overlay_map: dict[int, tuple[str, str | None]] = {}

    def apply_pair(search_text: str, replace_text: str, comment: str | None = None) -> None:
        search = normalize_space(search_text)
        replace = normalize_space(replace_text)
        if len(search) < MIN_PARAGRAPH_CHARS or not replace or search == replace:
            return
        for index, original_text in enumerate(body_paragraphs):
            if index in overlay_map:
                continue
            if search in original_text or anchor_in_policy(search, original_text):
                overlay_map[index] = (merge_replacement_with_anchor(search, replace), comment)
                return

    for edit in getattr(draft, "edits", None) or []:
        apply_pair(
            getattr(edit, "anchor_text", ""),
            getattr(edit, "replacement_text", ""),
            (getattr(edit, "citation_comment", None) or getattr(edit, "change_note", None) or None),
        )

    for section in getattr(draft, "sections", None) or []:
        apply_pair(
            getattr(section, "original_text", ""),
            getattr(section, "revised_text", ""),
            getattr(section, "change_note", None) or None,
        )

    target_excerpt = normalize_space(getattr(draft, "target_excerpt", "") or "")
    replacement_text = normalize_space(getattr(draft, "replacement_text", "") or "")
    if target_excerpt and replacement_text:
        apply_pair(
            target_excerpt,
            replacement_text,
            getattr(draft, "citation_comment", None) or None,
        )

    return overlay_map


def overlays_from_revision_alignment(
    body_paragraphs: list[str],
    revision_bytes: bytes,
) -> dict[int, str]:
    """Align internal paragraphs to the LLM revision DOCX by sequence matching."""
    internal_texts = body_paragraphs
    revision_texts = extract_revision_body_paragraphs(revision_bytes)
    if not internal_texts or not revision_texts:
        return {}

    overlay_map: dict[int, str] = {}
    matcher = difflib.SequenceMatcher(None, internal_texts, revision_texts)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            span = min(i2 - i1, j2 - j1)
            for offset in range(span):
                original = internal_texts[i1 + offset]
                revised = revision_texts[j1 + offset]
                # Require a non-empty replacement — never delete a line outright.
                if original != revised and normalize_space(revised):
                    overlay_map[i1 + offset] = revised
            continue
        # Skip pure deletes (tag == "delete") — preserve original policy lines.

    return overlay_map


def metadata_field_overlays(internal_bytes: bytes, revision_bytes: bytes) -> list[ParagraphOverlay]:
    """Track changes for revision metadata fields on the cover page."""
    internal_lines = labeled_metadata_lines(internal_bytes)
    revision_lines = labeled_metadata_lines(revision_bytes)
    overlays: list[ParagraphOverlay] = []
    for label in (
        "Version",
        "Effective Date",
        "Applicable Regulation",
        "Next Review Date",
        "Approved Date",
    ):
        original = internal_lines.get(label)
        revised = revision_lines.get(label)
        if not original or not revised:
            continue
        if normalize_space(original) == normalize_space(revised):
            continue
        overlays.append(ParagraphOverlay(original_text=original, revised_text=revised))
    return overlays


def build_metadata_field_overlays_for_internal_policy(
    internal_bytes: bytes,
    *,
    applicable_regulation: str | None = None,
) -> list[ParagraphOverlay]:
    """Update cover metadata fields when present on the Internal Policy.

    Only fields that already exist on the document are changed:
    - Version: bump minor (2.1 -> 2.2)
    - Effective Date: today's date
    - Applicable Regulation: merge existing with regulations cited in the redline
    - Next Review Date: two years from today
    - Approved / Approval date (and similar): today's date
    """
    from app.services.policy_revision_document import (
        bump_policy_version_label,
        policy_template_dates,
    )
    from app.services.redline_document import labeled_metadata_value, merge_applicable_regulation

    lines = labeled_metadata_lines(internal_bytes)
    if not lines:
        return []

    dates = policy_template_dates()
    overlays: list[ParagraphOverlay] = []

    if "Version" in lines:
        current = labeled_metadata_value(internal_bytes, "Version")
        bumped = bump_policy_version_label(current)
        revised = f"Version: {bumped}"
        if normalize_space(lines["Version"]) != normalize_space(revised):
            overlays.append(ParagraphOverlay(original_text=lines["Version"], revised_text=revised))

    if "Effective Date" in lines:
        revised = f"Effective Date: {dates['effective_date']}"
        if normalize_space(lines["Effective Date"]) != normalize_space(revised):
            overlays.append(
                ParagraphOverlay(original_text=lines["Effective Date"], revised_text=revised)
            )

    if "Applicable Regulation" in lines:
        existing = labeled_metadata_value(internal_bytes, "Applicable Regulation")
        # Merge cover-page citations with every RULE citation collected from redline comments.
        merged = merge_applicable_regulation(existing, applicable_regulation or "")
        revised = f"Applicable Regulation: {merged}"
        if normalize_space(lines["Applicable Regulation"]) != normalize_space(revised):
            overlays.append(
                ParagraphOverlay(
                    original_text=lines["Applicable Regulation"],
                    revised_text=revised,
                )
            )

    if "Next Review Date" in lines:
        revised = f"Next Review Date: {dates['next_review_date']}"
        if normalize_space(lines["Next Review Date"]) != normalize_space(revised):
            overlays.append(
                ParagraphOverlay(original_text=lines["Next Review Date"], revised_text=revised)
            )

    if "Approved Date" in lines:
        # Preserve the document's original label casing ("Approved date", "Approval Date", …).
        original = lines["Approved Date"]
        label = original.split(":", 1)[0].strip() or "Approved Date"
        revised = f"{label}: {dates['approved_date']}"
        if normalize_space(original) != normalize_space(revised):
            overlays.append(ParagraphOverlay(original_text=original, revised_text=revised))

    return overlays


def build_paragraph_overlays(
    internal_bytes: bytes,
    revision_bytes: bytes,
    draft: object,
) -> list[ParagraphOverlay]:
    """Build paragraph-level overlays from the internal policy and LLM revision."""
    document = Document(BytesIO(internal_bytes))
    body_paragraphs = collect_internal_body_paragraphs(document)
    if not body_paragraphs:
        return []

    body_index_by_text: dict[str, int] = {}
    for index, text in enumerate(body_paragraphs):
        body_index_by_text.setdefault(text, index)

    internal_policy_paragraphs = filter_policy_paragraphs_to_body(
        extract_policy_paragraphs(internal_bytes)
    )

    def map_policy_overlay(section_overlay: dict[int, str]) -> dict[int, str]:
        mapped: dict[int, str] = {}
        for policy_index, revised_text in section_overlay.items():
            if policy_index >= len(internal_policy_paragraphs):
                continue
            original = normalize_space(internal_policy_paragraphs[policy_index].text)
            if not _overlay_change_is_plausible(original, revised_text):
                continue
            if not normalize_space(revised_text):
                continue
            body_index = body_index_by_text.get(original)
            if body_index is not None:
                mapped[body_index] = revised_text
        return mapped

    merged: dict[int, str] = {}
    comments: dict[int, str] = {}
    edit_overlays = overlays_from_draft_edits(body_paragraphs, draft)
    for index, (revised_text, comment) in edit_overlays.items():
        if 0 <= index < len(body_paragraphs) and normalize_space(revised_text):
            if _looks_like_analysis_prose(revised_text):
                continue
            if _overlay_change_is_plausible(body_paragraphs[index], revised_text):
                merged[index] = revised_text
                if comment and normalize_space(comment):
                    comments[index] = normalize_space(comment)

    for source in (
        map_policy_overlay(overlays_from_draft_sections(internal_policy_paragraphs, draft)),
        overlays_from_revision_alignment(body_paragraphs, revision_bytes),
    ):
        for index, revised_text in source.items():
            if index not in merged and 0 <= index < len(body_paragraphs):
                if _looks_like_analysis_prose(revised_text):
                    continue
                if not normalize_space(revised_text):
                    continue
                if _overlay_change_is_plausible(body_paragraphs[index], revised_text):
                    merged[index] = revised_text

    overlays: list[ParagraphOverlay] = []
    for index, original_text in enumerate(body_paragraphs):
        revised_text = merged.get(index, original_text)
        if normalize_space(revised_text) == normalize_space(original_text):
            continue
        if not normalize_space(revised_text):
            continue
        if _looks_like_analysis_prose(revised_text):
            continue
        overlays.append(
            ParagraphOverlay(
                original_text=original_text,
                revised_text=revised_text,
                comment=comments.get(index),
            )
        )
    return overlays


def _tracked_runs_for_comment(paragraph: Paragraph) -> list[Run]:
    """Collect runs that Word can anchor a comment to after track-change markup."""
    insertion_runs = [Run(run, paragraph) for run in paragraph._p.xpath("./w:ins//w:r")]
    if insertion_runs:
        return insertion_runs
    deletion_runs = [Run(run, paragraph) for run in paragraph._p.xpath("./w:del//w:r")]
    if deletion_runs:
        return deletion_runs
    plain_runs = list(paragraph.runs)
    if plain_runs:
        return plain_runs
    return [Run(run, paragraph) for run in paragraph._p.xpath(".//w:r")]


def attach_change_justification_comment(
    document: DocumentObject,
    paragraph: Paragraph,
    comment_text: str,
    *,
    author: str = REDLINE_AUTHOR,
) -> bool:
    """Attach RULE/GAP/FIX justification as a Word comment on the tracked change."""
    from app.services.gap_analysis_service import normalize_rule_gap_fix_comment

    text = normalize_rule_gap_fix_comment(comment_text)
    if not text:
        return False
    runs = _tracked_runs_for_comment(paragraph)
    if not runs:
        return False
    initials = REDLINE_AUTHOR_INITIALS if author == REDLINE_AUTHOR else "".join(
        part[0] for part in author.split() if part
    )[:3].upper() or "RA"
    try:
        document.add_comment(
            runs=runs,
            text=text[:12_000],
            author=author,
            initials=initials,
        )
    except Exception as exc:
        logger.warning(
            "tracked_redline_comment_attach_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
        )
        return False
    return True


def apply_paragraph_overlays(
    internal_bytes: bytes,
    overlays: list[ParagraphOverlay],
    *,
    author: str = REDLINE_AUTHOR,
    gap_summary: str = "",
) -> bytes:
    """Replace internal policy paragraphs with tracked insertions/deletions.

    Every substantive tracked correction receives a Word comment containing the
    RULE / GAP / FIX justification (prefer overlay.comment; never reuse one shared
    comment across every change unless no per-overlay comment exists).
    """
    revision_doc = RevisionDocument(BytesIO(internal_bytes))
    enable_track_revisions(revision_doc.document)
    cover_boundary = _cover_body_boundary_index(revision_doc.document)
    default_comment = gap_summary.strip()[:2_000] or None
    used_comments: set[str] = set()

    applied = 0
    comments_attached = 0
    for overlay in overlays:
        if _looks_like_analysis_prose(overlay.revised_text) and not _is_metadata_overlay(overlay):
            continue
        paragraph = _find_paragraph_for_overlay(revision_doc.document, overlay)
        if paragraph is None:
            continue
        if not _is_metadata_overlay(overlay):
            if paragraph_index(revision_doc.document, paragraph) < cover_boundary:
                continue
        revision_paragraph = RevisionParagraph.from_paragraph(paragraph)
        current_text = revision_paragraph.text
        revised_text = normalize_space(overlay.revised_text)
        current_normalized = normalize_space(current_text)
        if not revised_text:
            # Never delete a whole line without a replacement.
            continue
        if not current_normalized:
            continue
        if revised_text == current_normalized:
            continue
        # Metadata bumps (version/date) do not need RULE/GAP/FIX side comments.
        comment = None if _is_metadata_overlay(overlay) else (overlay.comment or None)
        if comment:
            comment_key = " ".join(comment.lower().split())
            if comment_key in used_comments:
                comment = None
            else:
                used_comments.add(comment_key)
        elif default_comment and not _is_metadata_overlay(overlay):
            # Use the shared fallback only when no unique overlay comment was supplied,
            # and only once so every change does not get the same RULE/GAP/FIX blob.
            comment_key = " ".join(default_comment.lower().split())
            if comment_key not in used_comments:
                comment = default_comment
                used_comments.add(comment_key)

        revision_paragraph.replace_tracked_at(
            0,
            len(current_text),
            revised_text,
            author=author,
        )
        if comment and attach_change_justification_comment(
            revision_doc.document,
            paragraph,
            comment,
            author=author,
        ):
            comments_attached += 1
        applied += 1

    logger.info(
        "tracked_redline_overlay_applied",
        paragraph_changes=applied,
        justification_comments=comments_attached,
    )
    buffer = BytesIO()
    revision_doc.save(buffer)
    return buffer.getvalue()


def apply_tracked_redline_overlay(
    internal_bytes: bytes,
    *,
    replacements: list[TrackedReplacement],
    author: str = REDLINE_AUTHOR,
    gap_summary: str = "",
) -> bytes:
    """Apply targeted sentence edits with unique RULE/GAP/FIX Word comments."""
    revision_doc = RevisionDocument(BytesIO(internal_bytes))
    enable_track_revisions(revision_doc.document)
    default_comment = gap_summary.strip()[:2_000] or None

    applied = 0
    comments_attached = 0
    used_comments: set[str] = set()

    for replacement in replacements:
        search = normalize_space(replacement.search_text)
        replace = normalize_space(replacement.replace_text)
        # Cover metadata (Version / dates / Applicable Regulation / etc.) is owned by
        # build_metadata_field_overlays_for_internal_policy — never LLM sentence edits.
        if is_cover_or_metadata_line(search) or is_cover_or_metadata_line(replace):
            continue
        comment = (replacement.comment or default_comment or "").strip() or None
        if comment:
            comment_key = " ".join(comment.lower().split())
            if comment_key in used_comments:
                comment = None
            else:
                used_comments.add(comment_key)

        if search:
            count = revision_doc.find_and_replace_tracked(
                search,
                replace,
                author=author,
                comment=None,
                index_mode="text",
            )
            if not count:
                count = revision_doc.find_and_replace_tracked(
                    search,
                    replace,
                    author=author,
                    comment=None,
                    index_mode="accepted",
                )
            if count:
                applied += count
                if comment:
                    needle = normalize_space(replace)
                    for paragraph in revision_doc.document.paragraphs:
                        revision_paragraph = RevisionParagraph.from_paragraph(paragraph)
                        if not revision_paragraph.has_track_changes:
                            continue
                        accepted = normalize_space(
                            getattr(revision_paragraph, "accepted_text", paragraph.text)
                        )
                        if needle and needle not in accepted and search not in accepted:
                            continue
                        if attach_change_justification_comment(
                            revision_doc.document,
                            paragraph,
                            comment,
                            author=author,
                        ):
                            comments_attached += 1
                            break
            continue

        if replace:
            if _looks_like_analysis_prose(replace):
                continue
            body_paragraphs = [
                paragraph
                for paragraph in revision_doc.document.paragraphs
                if normalize_space(paragraph.text)
            ]
            if not body_paragraphs:
                continue
            target = body_paragraphs[0]
            target_text = normalize_space(target.text)
            if target_text == replace:
                continue
            count = revision_doc.find_and_replace_tracked(
                target_text,
                replace,
                author=author,
                comment=None,
                index_mode="text",
            )
            if count:
                applied += count
                if comment and attach_change_justification_comment(
                    revision_doc.document,
                    target,
                    comment,
                    author=author,
                ):
                    comments_attached += 1

    logger.info(
        "tracked_redline_sentence_edits_applied",
        replacements=len(replacements),
        applied=applied,
        justification_comments=comments_attached,
    )
    buffer = BytesIO()
    revision_doc.document.save(buffer)
    return buffer.getvalue()


def docx_has_track_changes(document_bytes: bytes) -> bool:
    """Return True when the DOCX contains Word track-change markup."""
    with BytesIO(document_bytes) as buffer:
        revision_doc = RevisionDocument(buffer)
        if revision_doc.track_changes:
            return True
    document = Document(BytesIO(document_bytes))
    settings = document.settings.element
    track_flag = settings.find(qn("w:trackRevisions"))
    if track_flag is not None and track_flag.get(qn("w:val")) == "true":
        return True
    return b"w:ins" in document_bytes or b"w:del" in document_bytes


def extract_accepted_redline_text(document_bytes: bytes) -> str:
    """Return the proposed policy text as if all tracked changes were accepted.

    python-docx ``paragraph.text`` often omits tracked insertions/deletions, which would
    make redline-vs-internal cosine similarity compare mostly unchanged original lines and
    inflate the score. This helper reads accepted text so embeddings reflect the LLM redline.
    """
    document = Document(BytesIO(document_bytes))
    lines: list[str] = []
    for paragraph in iter_all_paragraphs(document):
        revision_paragraph = RevisionParagraph.from_paragraph(paragraph)
        if revision_paragraph.has_track_changes:
            text = normalize_space(
                str(getattr(revision_paragraph, "accepted_text", "") or "")
            )
        else:
            text = normalize_space(paragraph.text)
        if text:
            lines.append(text)
    return "\n".join(lines)


def strip_word_comments(document: DocumentObject) -> None:
    """Remove Word comment markers and clear the comments part (approve-all cleanup)."""
    body = document.element.body
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        for node in list(body.iter(qn(f"w:{tag}"))):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
    comments_part = getattr(document.part, "_comments_part", None)
    if comments_part is None:
        return
    root = comments_part.element
    for child in list(root):
        root.remove(child)


def accept_all_tracked_changes(redline_bytes: bytes) -> bytes:
    """Mass-accept every tracked change and drop justification comments → clean DOCX."""
    revision_doc = RevisionDocument(BytesIO(redline_bytes))
    revision_doc.accept_all()
    strip_word_comments(revision_doc.document)
    # Turn off track-revisions so the uploaded file is a normal policy document.
    settings = revision_doc.document.settings.element
    track_flag = settings.find(qn("w:trackRevisions"))
    if track_flag is not None:
        settings.remove(track_flag)
    buffer = BytesIO()
    revision_doc.save(buffer)
    return buffer.getvalue()


# Backward-compatible exports used in tests.
def extract_substantive_paragraphs(document_bytes: bytes) -> list[str]:
    document = Document(BytesIO(document_bytes))
    return collect_internal_body_paragraphs(document)


def replacements_from_draft_edits(draft: object) -> list[TrackedReplacement]:
    replacements: list[TrackedReplacement] = []
    seen: set[str] = set()

    def add_pair(search_text: str, replace_text: str, comment: str | None = None) -> None:
        search = normalize_space(search_text)
        replace = normalize_space(replace_text)
        if len(search) < MIN_PARAGRAPH_CHARS or not replace or search == replace or search in seen:
            return
        seen.add(search)
        replacements.append(
            TrackedReplacement(search_text=search, replace_text=replace, comment=comment)
        )

    for edit in getattr(draft, "edits", None) or []:
        add_pair(
            getattr(edit, "anchor_text", ""),
            getattr(edit, "replacement_text", ""),
            getattr(edit, "citation_comment", None) or getattr(draft, "drafting_summary", None),
        )
    for section in getattr(draft, "sections", None) or []:
        add_pair(
            getattr(section, "original_text", ""),
            getattr(section, "revised_text", ""),
            getattr(section, "change_note", None) or getattr(draft, "drafting_summary", None),
        )
    return replacements


def replacements_from_revision_diff(
    internal_bytes: bytes,
    revision_bytes: bytes,
) -> list[TrackedReplacement]:
    internal_document = Document(BytesIO(internal_bytes))
    internal_items = collect_internal_body_paragraphs(internal_document)
    revision_texts = extract_revision_body_paragraphs(revision_bytes)
    if not internal_items or not revision_texts:
        return []

    replacements: list[TrackedReplacement] = []
    matcher = difflib.SequenceMatcher(None, internal_items, revision_texts)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        span = min(i2 - i1, j2 - j1)
        for offset in range(span):
            search = internal_items[i1 + offset]
            replace = revision_texts[j1 + offset]
            if search != replace:
                replacements.append(TrackedReplacement(search_text=search, replace_text=replace))
    return replacements


def merge_replacements(*groups: list[TrackedReplacement]) -> list[TrackedReplacement]:
    merged: list[TrackedReplacement] = []
    seen_search: set[str] = set()
    for group in groups:
        for replacement in group:
            search = normalize_space(replacement.search_text)
            if search and search in seen_search:
                continue
            if search:
                seen_search.add(search)
            merged.append(replacement)
    merged.sort(key=lambda item: len(item.search_text), reverse=True)
    return merged
