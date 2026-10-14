"""Shared constants for the Marketing Review Agent (Release 2)."""

from __future__ import annotations

from typing import Final

REPORT_HEADER: Final[str] = (
    'This is a review for the CCO’s consideration. It is not an approval. '
    'Nothing goes out until the CCO signs off.'
)
PERFORMANCE_DISCLAIMER: Final[str] = (
    'Figures are reviewed for presentation compliance only. '
    'Accuracy of underlying performance is not verified.'
)

FORMATS: Final[frozenset[str]] = frozenset({
    'webpage',
    'presentation',
    'social',
    'email',
    'blog',
    'text',
    'image',
})
AUDIENCES: Final[frozenset[str]] = frozenset({
    'retail',
    'prospect',
    'institutional',
    'mixed',
    'unknown',
})
REGISTRANT_TYPES: Final[frozenset[str]] = frozenset({
    'ria',
    'broker_dealer',
    'dual',
    'unknown',
})
CONTENT_TAGS: Final[tuple[str, ...]] = (
    'performance',
    'hypothetical_performance',
    'benchmark_comparison',
    'testimonial',
    'endorsement',
    'third_party_rating',
    'structured_products',
    'scarcity',
    'safety_claim',
    'strategy_promotion',
)
OPTIONS: Final[frozenset[str]] = frozenset({
    'redline',
    'add_disclosure',
    'pull',
    'do_nothing',
    'escalate',
    'held',
})
ALLOWED_UPLOAD_TYPES: Final[frozenset[str]] = frozenset({
    'text/plain',
    'text/html',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
    'application/vnd.ms-powerpoint',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/msword',
    'image/png',
    'image/jpeg',
    'image/webp',
})
ALLOWED_UPLOAD_SUFFIXES: Final[frozenset[str]] = frozenset({
    '.txt',
    '.html',
    '.htm',
    '.pdf',
    '.pptx',
    '.doc',
    '.docx',
    '.png',
    '.jpg',
    '.jpeg',
    '.webp',
})
MARKETING_RULE_KEYWORDS: Final[tuple[str, ...]] = (
    'marketing',
    'advertis',
    'testimonial',
    'endorsement',
    'performance advertising',
    'communications with the public',
    '2210',
    '2211',
    '2212',
    '2213',
    '2214',
    '2215',
    '2216',
    '2010',
    '206(4)-1',
    '204-2',
    'third-party rating',
    'hypothetical performance',
    'variable annuit',
    'fund ranking',
)
MARKETING_RULE_SET: Final[str] = """# Which rules apply:
One main rule per regulator, with extra rules that switch on depending on what's in the ad.

## SEC — for RIA clients (primary)

• Rule 206(4)-1, the Marketing Rule. The main one. Covers honesty, testimonials,
  endorsements, third-party ratings, and performance.

• Rule 204-2, recordkeeping. Not a check — a build requirement. Every review has
  to be saved and findable, including which version of the rule was used.

## FINRA — for broker-dealer clients (secondary)

• Rule 2210, communications with the public. The main one. Also covers who has to
  approve an ad, what has to be filed, and what has to be kept.

• Rules 2211–2216. Switch on by topic: variable annuities (2211), fund rankings
  (2212), bond fund volatility ratings (2213), investment-analysis tools (2214),
  security futures (2215), mortgage obligations (2216).

• Rule 2010, fair dealing. The catch-all standard underneath everything."""

CONTENT_STATUS_QUEUED = 'queued'
CONTENT_STATUS_IN_REVIEW = 'in_review'
CONTENT_STATUS_AWAITING_ANSWERS = 'awaiting_answers'
CONTENT_STATUS_REVIEWED = 'reviewed'
CONTENT_STATUS_FAILED = 'failed'

REVIEW_QUEUED = 'queued'
REVIEW_RUNNING = 'running'
REVIEW_AWAITING_ANSWERS = 'awaiting_answers'
REVIEW_READY = 'ready_for_cco'
REVIEW_SIGNED_OFF = 'signed_off'
REVIEW_FAILED = 'failed'

MAX_BODY_CHARS = 80_000
MAX_RULE_EXCERPT_CHARS = 24_000
MAX_QUESTIONS = 6
MAX_ARCHIVE_RECHECK = 40
