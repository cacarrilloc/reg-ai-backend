"""Policy library type constants and helpers."""

from __future__ import annotations

from fastapi import HTTPException, status

INTERNAL_POLICY_TYPE = 'uploaded'
LEGACY_INTERNAL_POLICY_TYPES = frozenset({'WSP', 'uploaded'})
REGULATORY_REFERENCE_TYPE = 'REGULATORY_REFERENCE'
MARKETING_CONTENT_TYPE = 'MARKETING_CONTENT'
REGULATORY_PUBLICATION_CHUNK_TYPE = 'REGULATORY_PUBLICATION'
CONTEXT_DOCUMENT_CHUNK_TYPE = 'CONTEXT_DOCUMENT'
REDLINE_CHUNK_TYPE = 'REDLINE'
INTERNAL_REGULATOR_POLICY_NAMES = frozenset({'FINRA', 'SEC', 'CFTC'})
INTERNAL_REGULATOR_POLICY_OPTIONS: tuple[str, ...] = ('FINRA', 'SEC', 'CFTC')


def validate_internal_regulator_policy_name(value: str) -> str:
    """Normalize an internal policy name for upload (free-text; no longer regulator-restricted)."""
    normalized = ' '.join(value.split()).strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail='Policy name cannot be empty.',
        )
    return normalized[:180]


def resolve_internal_regulator_source(policy_name: str) -> str | None:
    """Return FINRA, SEC, or CFTC when a policy/reference name identifies that regulator.

    Exact names (FINRA/SEC/CFTC) and prefixed reference docs (e.g. ``FINRA Regulations``,
    ``SEC Newsroom Reference``) all resolve to the regulator.
    """
    normalized = ' '.join((policy_name or '').split()).strip().upper()
    if normalized in INTERNAL_REGULATOR_POLICY_NAMES:
        return normalized
    for source in INTERNAL_REGULATOR_POLICY_OPTIONS:
        if normalized.startswith(f'{source} '):
            return source
    return None


def is_internal_policy_type(policy_type: str | None) -> bool:
    normalized = (policy_type or INTERNAL_POLICY_TYPE).strip().upper()
    return normalized not in {REGULATORY_REFERENCE_TYPE, MARKETING_CONTENT_TYPE}


def is_internal_document_type(document_type: str | None) -> bool:
    """True for uploaded Internal policies and legacy WSP chunk/policy labels."""
    if document_type is None:
        return True
    return document_type.strip() in LEGACY_INTERNAL_POLICY_TYPES


def policy_type_label(policy_type: str | None) -> str:
    if (policy_type or '').strip().upper() == REGULATORY_REFERENCE_TYPE:
        return 'Regulation'
    return 'Internal'


def chunk_document_type_for_policy(policy_type: str | None) -> str:
    """Map a policy row type to the document_chunks.document_type value."""
    normalized = (policy_type or INTERNAL_POLICY_TYPE).strip().upper()
    if normalized == REGULATORY_REFERENCE_TYPE:
        return REGULATORY_REFERENCE_TYPE
    return INTERNAL_POLICY_TYPE


def normalize_chunk_policy_name(policy_name: str, policy_type: str | None = None) -> str:
    """Normalize the document_chunks.policy_name value.

    Internal policies keep the user's Policy name field value (whitespace-normalized).
    Government reference docs map to FINRA/SEC/CFTC for regulator-scoped retrieval.
    """
    display_name = ' '.join((policy_name or '').split()).strip()
    normalized_type = (policy_type or INTERNAL_POLICY_TYPE).strip().upper()
    if normalized_type != REGULATORY_REFERENCE_TYPE:
        return display_name

    regulator = resolve_internal_regulator_source(display_name)
    if regulator is not None:
        return regulator
    upper_name = display_name.upper()
    for source in INTERNAL_REGULATOR_POLICY_OPTIONS:
        if source in upper_name:
            return source
    return display_name
