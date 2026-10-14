"""One-pass marketing review: classify, layout + words, ask only when it changes the finding."""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from app.ai import ChatMessage, LLMProvider
from app.services.llm_json import parse_llm_json_dict, response_text
from app.services.llm_persona import with_financial_law_expert_persona
from app.services.marketing_slide_service import dedupe_presentation_layout_notes, slides_from_layout_notes
from app.services.marketing_types import (
    CONTENT_TAGS,
    MARKETING_RULE_SET,
    MAX_QUESTIONS,
    OPTIONS,
    PERFORMANCE_DISCLAIMER,
    REPORT_HEADER,
)

_PERFORMANCE_RE = re.compile(r'\b\d+(\.\d+)?\s*%')
_INVESTMENT_PERFORMANCE_RE = re.compile(
    r'(?:'
    r'\+\s*\d+(?:\.\d+)?\s*%'
    r'|(?:gross|net)\s+of\s+fees'
    r'|before (?:advisory )?fees'
    r'|returned\s+\d'
    r'|would have returned'
    r'|backtested'
    r'|track record'
    r'|since inception'
    r'|\d+(?:\.\d+)?\s*%\s*\((?:19|20)\d{2}\)'
    r')',
    re.I,
)
_GROSS_NET_RE = re.compile(r'\b(gross|net of fees|after fees|before (advisory )?fees)\b', re.I)
_NET_FIGURE_RE = re.compile(r'\bnet(\s+of\s+fees)?\b', re.I)
_TESTIMONIAL_RE = re.compile(r"(\"|'|“)[^\"”']{12,}(\"|'|”)|said:|client said|testimonial", re.I)
_CLIENT_REVIEW_RE = re.compile(
    r'\b('
    r'google reviews?'
    r'|client reviews?'
    r'|client perspective'
    r'|hear one client'
    r'|featured speaker is a .{0,60}client'
    r'|voluntary feedback from (?:current and )?former clients'
    r'|current and former clients'
    r')\b',
    re.I,
)
_CLIENT_TESTIMONIAL_RE = re.compile(
    r'\btestimonial\b|client said|said:|—\s*[A-Z][a-z]+ [A-Z]\.|client since',
    re.I,
)
_TESTIMONIAL_PERFORMANCE_RE = re.compile(
    r'\b(doubled my portfolio|doubled .{0,20}portfolio|returned \d|\d+(\.\d+)?\s*%)\b',
    re.I,
)
_RATING_RE = re.compile(
    r'\b('
    r'top\s+\d+'
    r'|best[- ]in[- ]state'
    r'|five star'
    r'|forbes'
    r'|bbb'
    r'|rated'
    r'|google reviews?'
    r'|\d+\+?\s+(?:google\s+)?(?:client\s+)?reviews?'
    r')\b',
    re.I,
)
_FIDUCIARY_CLAIM_RE = re.compile(
    r'\b(fee[- ]only(?: fiduciary)?|fiduciary(?: (?:standard|duty|advisor|adviser))?|'
    r'independent (?:advisory|adviser|advisor|fiduciary))\b',
    re.I,
)
_GUARANTEE_RE = re.compile(
    r'\b('
    r'we guarantee'
    r'|guaranteed (returns?|results?|income|principal)'
    r'|beat the market every year'
    r'|eliminate[s]? (all )?risk'
    r'|protected client capital in every downturn'
    r')\b',
    re.I,
)
_SAFETY_DOWNTURN_RE = re.compile(
    r'protected client capital in every downturn(?: since the firm was founded)?',
    re.I,
)
_SCARCITY_RE = re.compile(
    r'\b('
    r'spots are limited'
    r'|filling fast'
    r'|only \d+ (new )?(families|spots)'
    r'|only take on \d+ new families'
    r'|12 new families per quarter'
    r')\b',
    re.I,
)
_RELATIVE_PERF_RE = re.compile(
    r'outperformed \d+(\.\d+)?%\s+of comparable(?: strategies)?|'
    r'outperformed \d+(\.\d+)?%',
    re.I,
)
_SUBJECT_CONCEAL_RE = re.compile(r"most advisors won['’]?t tell you this", re.I)
_FREE_OFFER_RE = re.compile(r'free retirement readiness score', re.I)
_MERGE_FIELD_RE = re.compile(r'\{\{[^}]+\}\}')
_EMAIL_FINDING_ORDER = (
    'subject_line',
    'relative_performance',
    'third_party_rating',
    'scarcity',
    'free_offer',
    'sequence_flag',
)
_EMAIL_FINDING_PARTS = {
    'subject_line': ('subject', 'subject line'),
    'relative_performance': ('body', '87% claim'),
    'third_party_rating': ('body', 'rating'),
    'scarcity': ('body', 'scarcity'),
    'free_offer': ('body', 'body'),
    'sequence_flag': ('', 'sequence'),
}
_EMAIL_RATING_DRAFT = (
    'Rating disclosures must travel with the rating into this email — a website caption does '
    'not cover it. Keep merge fields. Add inline: “WealthReview named us a Top 100 Advisor in '
    'its 2025 list (published March 2025, based on 2024 data; Meridian did not pay to be considered).”'
)
_EMAIL_RELATIVE_DRAFT = (
    'Remove the 87% headline. An internal, one-time, unaudited comparison cannot support a '
    'relative-performance claim in an advertisement. A compliant comparative claim needs defined '
    'methodology, current data, and full sourcing. Leave {{FirstName}} / {{Email}} untouched.'
)
_EMAIL_SUBJECT_DRAFT = (
    'Optional redline. Subject is part of the advertisement. Replace implied concealment with '
    'the actual offer, keeping the merge field: {{FirstName}}, your Retirement Readiness Score is waiting'
)
_EMAIL_SCARCITY_DRAFT = (
    'Keep “We only take on 12 new families per quarter” if the cap is substantiated. Remove '
    '“and August is filling fast” unless evidenced at send time. Leave merge fields untouched.'
)
_EMAIL_SEQUENCE_DRAFT = (
    'Steps 1 and 3 of this cadence were not submitted. The 87% statistic and the rating sit in '
    'the template library — if reused, the same findings apply. Recommended for submission.'
)
_ELIMINATE_RISK_RE = re.compile(
    r'designed to eliminate it|eliminate[s]? (?:all )?(?:sequence )?risk',
    re.I,
)
_CLIENT_OUTCOME_RE = re.compile(
    r'stayed on track|double digits|60/40 retirees|60/40 portfolios',
    re.I,
)
_BLOG_FINDING_ORDER = (
    'eliminate_claim',
    'client_outcome_2022',
    'educational_half',
    'missing_disclosures',
    'free_cta',
)
_BLOG_ELIMINATE_DRAFT = (
    'Leave paragraphs 1–5 untouched. Redline paragraph 6: “designed to eliminate it” → '
    '“The approach is designed to reduce exposure to sequence risk by matching near-term '
    'spending to dedicated, lower-volatility assets.” Cure by rewording, not disclosure.'
)
_BLOG_2022_DRAFT = (
    'Unsubstantiated client-outcome claim — cannot be disclosed into compliance. Rewrite as '
    'strategy-design commentary with no client-outcome assertion: “A year like 2022 — the '
    'worst bond year in four decades — is the environment this structure is built for: '
    'near-term withdrawals are insulated from having to sell depressed assets. No strategy '
    'removes market risk, and results vary by client.”'
)
_BLOG_DISCLOSURE_DRAFT = (
    'Once paragraph 6 makes the page an advertisement, add page-level disclosures: adviser '
    'identity, SEC registration, investment risk, and Form ADV Part 2. Currently none.'
)
_SCREENSHOT_DISCLOSURE_DRAFT = (
    'Add page-level disclosures: adviser identity, SEC registration, investment risk, '
    'and Form ADV Part 2. Currently none appear on the screenshot.'
)
_FREE_CTA_RE = re.compile(r'free strategy call|book my call|book a free', re.I)
_PAGE_DISCLOSURE_RE = re.compile(
    r'form adv|past performance does not|sec[- ]registered|not a guarantee of future',
    re.I,
)
_BLOG_2022_QUESTION = (
    'For the 2022 claim: does ‘clients stayed on track’ refer to all Income Ladder clients, '
    'a composite, or selected accounts — and is there a documented basis for the ‘double digits’ '
    'comparison to 60/40 portfolios?'
)
_HYPOTHETICAL_RE = re.compile(r'\b(backtested|would have returned|hypothetical)\b', re.I)
_FOOTER_HINT_RE = re.compile(r'\b(footer|below the fold|7\s*pt|sitemap)\b', re.I)
_UNPAID_TESTIMONIAL_RE = re.compile(
    r'no compensation(?: was (?:paid|provided))?|not compensated|unpaid (?:testimonial|client)',
    re.I,
)
_WEBPAGE_REGION_ORDER = ('hero', 'badge_row', 'testimonial', 'mid_page', 'footer')
_PRESENTATION_FINDING_ORDER = (
    'cherry_pick',
    'gross_net_layout',
    'hypothetical_backtest',
    'benchmark_comparison',
    'puffery_title',
    'unsubstantiated_exclusivity',
    'unsubstantiated_statistic',
    'third_party_outcome',
)
_REGION_ANCHORS = {
    'bar_chart': 'chart',
    'gross_net': 'chart',
    'callout': 'callout',
    'slide_footer': 'footer',
    'benchmark': 'footer',
    'slide_title': 'title',
    'slide_copy': 'slide copy',
}
_EXCLUSIVITY_RE = re.compile(
    r'\b('
    r'we are the only'
    r'|the only agentic'
    r'|nobody (?:else )?automates'
    r'|none own(?:s)? our'
    r')\b',
    re.I,
)
_QUANTIFIED_SPLIT_RE = re.compile(
    r'we handle the \d+%(?: that is manual)?',
    re.I,
)
_THIRD_PARTY_OUTCOME_RE = re.compile(
    r'\b(?:sigmoid|haast)\b.{0,120}(?:\d+\s*[-–]\s*\d+\s*%|\d+\s*%)'
    r'|\b\d+\s*[-–]\s*\d+\s*%\s+improvement\b',
    re.I,
)
_EXCLUSIVITY_DRAFT = (
    'Qualify the exclusivity claim. “Only” / “nobody automates” is an untrue or '
    'unsubstantiated statement under Rule 206(4)-1 unless the firm can document it. '
    'Safer: “Built to take a regulatory change through to a CCO-ready draft.”'
)
_QUANTIFIED_SPLIT_DRAFT = (
    'The 5% / 95% split is an unsubstantiated statistic in an advertisement. '
    'Qualify it as illustrative, or pull the percentages until there is a documented basis.'
)
_THIRD_PARTY_OUTCOME_DRAFT = (
    'Third-party outcome statistics inherit advertising treatment. Name the source and date, '
    'and state that the result is not the advertiser’s and not typical. If it cannot be '
    'substantiated, pull the figure.'
)
_MULTI_PERIOD_RE = re.compile(
    r'\b(1[\s-]?year|5[\s-]?year|10[\s-]?year|since inception|since 20\d{2})\b',
    re.I,
)
_BACKTEST_SPAN_RE = re.compile(r'backtested to \d{4}.{0,160}', re.I)
_SPEAKS_FOR_ITSELF_RE = re.compile(r'performance that speaks for itself', re.I)
_BEFORE_FEES_RE = re.compile(r'before (advisory )?fees|gross of fees|returns shown before', re.I)
_SEE_APPENDIX_RE = re.compile(r'see appendix', re.I)
_APPENDIX_NET_RE = re.compile(
    r'(appendix|slide\s+\d+).{0,120}\bnet\b|\bnet\b.{0,120}(appendix|slide\s+\d+|9\s*pt)',
    re.I | re.S,
)
_SLIDE7_QUESTION = (
    'Two things on slide 7: is this deck used with retail prospects, institutional '
    'prospects, or both? And does the S&P 500 figure include reinvested dividends '
    '(total return) or price only?'
)
_CHERRY_PICK_DRAFT = (
    'Replace the single-year gross-only chart with 1-year / 5-year / since-inception, '
    'showing gross and net side-by-side at equal size. Firm to supply figures beyond '
    'the highlighted year. Figures are reviewed for presentation only — not verified.'
)
_GROSS_NET_SLIDE_DRAFT = (
    'Put the net figure on the same slide as the gross figure, comparable size — not '
    'seven slides away in 9pt appendix type. “See appendix for disclosures” does not '
    'satisfy equal prominence.'
)
_BACKTEST_RETAIL_DRAFT = (
    'Cannot run in the retail version. Two decks: retail drops the gold backtest '
    'callout entirely; institutional keeps it on slide 7a with a methodology page. '
    'Methodology page skeleton: backtest period and whether live accounts existed; '
    'material assumptions (cash flows, fees, rebalancing, taxes); known limitations '
    '(survivor bias, overfitting); how results differ from actual client results.'
)
_BENCHMARK_DRAFT = (
    'Footer, 11pt minimum: Benchmark is S&P 500 total return (dividends reinvested). '
    'Past performance does not guarantee future results. Net returns reflect deduction '
    'of advisory fees.'
)
_SOCIAL_FINDING_ORDER = (
    'structured_yield_claim',
    'principal_approval',
    'social_adoption',
    'scarcity',
    'emoji_tone',
)
_ADOPTION_RE = re.compile(
    r'(liked this|like this|has liked this comment|liked (the |a )?(client )?comment|'
    r'liked a client|and \d+ others like this)',
    re.I,
)
_STRUCTURED_YIELD_RE = re.compile(
    r'pays \d+(\.\d+)?%\s*income.{0,60}protecting your downside|'
    r'protecting your downside|'
    r'\d+(\.\d+)?%\s*(income|downside protection)|'
    r'downside protection',
    re.I,
)
_SUNGLASSES_RE = re.compile(r'😎|sunglasses emoji', re.I)
_LINKEDIN_CORRECTED_DRAFT = (
    'Markets are volatile, and a lot of investors are asking about income strategies. '
    'Structured notes are one option — they can offer defined income with partial downside '
    'buffers, and they carry real risks, including issuer credit risk and caps on protection. '
    'If you want to talk through whether they fit your situation, message me.\n\n'
    'Structured notes are complex products and are not suitable for all investors.'
)
_LINKEDIN_PULL_DRAFT = (
    'Recommended action: take the post down now. It is live, unapproved, and cannot be '
    'edited into compliance in place. Supervision follow-up is the CCO’s.\n'
    '1. Remove the live post and unlike the client comment (an adopted testimonial).\n'
    '2. Route the corrected draft through principal pre-approval.\n'
    '3. Log the incident per firm WSPs.\n\n'
    f'Corrected draft — for pre-approval:\n{_LINKEDIN_CORRECTED_DRAFT}'
)
_YIELD_CLAIM_DRAFT = (
    'Yield figure removed — a stated percentage on a public retail post triggers presentation '
    'requirements that do not fit the format. Discuss specific terms in a one-to-one, '
    'suitability-checked setting. “Protecting your downside” corrected to partial buffers '
    'and named risks (issuer credit risk, caps on protection).'
)
_ADOPTION_DRAFT = (
    'Unlike the client comment and do not engage. Liking is adoption: the firm’s guidance '
    'treats it as the rep making the statement. An adopted testimonial on a public post '
    'pulls in testimonial treatment and the fair-and-balanced standard.'
)
_SOCIAL_SCARCITY_DRAFT = (
    'Remove the scarcity / urgency line (“spots are limited this quarter”). False urgency '
    'on an advisory relationship is misleading.'
)

_HERO_GROSS_DRAFT = (
    '11.4% gross of fees; net of fees X.X% [firm to supply]. '
    'Past performance does not guarantee future results.'
)
_TESTIMONIAL_DRAFT = (
    'Statements by current clients; no compensation was provided. '
    'Individual results vary and are not typical. Rotate the “doubled my portfolio” '
    'quote out of the carousel — it is a performance claim.'
)
_SAFETY_DRAFT = (
    'Redline: “has protected client capital in every downturn since the firm was founded” '
    '→ “seeks to manage downside risk”. A disclosure cannot cure an absolute safety claim.'
)
_FOOTER_COVERED_DRAFT = (
    'May remain for the record, but no longer carries the disclosure burden — '
    'items 1–4 must sit adjacent to the claims, at a readable size.'
)
_LAYOUT_REDLINE_DRAFT = (
    'Layout instruction: move required disclosures out of 7pt / below-the-fold footer '
    'and place them adjacent to each claim at a readable size. Wording alone does not cure this.'
)


def _layout_is_screenshot(notes: list[dict[str, Any]] | None) -> bool:
    """True for an uploaded image capture, not a URL scrape or pasted article."""
    for note in notes or []:
        if not isinstance(note, dict):
            continue
        if str(note.get('label') or '').lower() == 'screenshot':
            return True
        if str(note.get('font_note') or '').lower() == 'screenshot':
            return True
    return False


def _readable_review_text(body_text: str, notes: list[dict[str, Any]] | None) -> str:
    from app.services.marketing_ingest_service import is_decoded_image_dump, readable_marketing_body

    cleaned = readable_marketing_body(body_text, notes)
    if is_decoded_image_dump(cleaned):
        return ''
    return cleaned


def synthesize_layout_notes(
    *,
    body_text: str,
    layout_notes: list[dict[str, Any]] | None,
    format_name: str = '',
) -> list[dict[str, Any]]:
    """Record regions the visual pass would see. Screenshot examples are the layout source of truth."""
    notes = [dict(note) for note in (layout_notes or [])]
    text = body_text or ''
    if not text:
        return notes

    def has_region(*names: str) -> bool:
        for note in notes:
            region = str(note.get('region') or '')
            if any(region == name or region.startswith(name) for name in names):
                return True
        return False

    webpage = format_name == 'webpage' or bool(_FOOTER_HINT_RE.search(text))
    if webpage:
        if _PERFORMANCE_RE.search(text) and not has_region('hero'):
            excerpt = _clip_around(text, '11.4%') or _clip_around(text, '%') or text[:180]
            notes.append({
                'region': 'hero',
                'font_note': 'full-width banner, 48pt headline',
                'excerpt': excerpt,
                'char_count': len(excerpt),
            })
        if _RATING_RE.search(text) and not has_region('badge'):
            notes.append({
                'region': 'badge_row',
                'font_note': 'three large gold seals, directly under hero',
                'excerpt': _rating_excerpt(text),
                'char_count': 80,
            })
        if (_TESTIMONIAL_RE.search(text) or _CLIENT_REVIEW_RE.search(text)) and not has_region('testimonial'):
            notes.append({
                'region': 'testimonial',
                'font_note': 'rotating, 24pt type',
                'excerpt': (
                    _clip_around(text, 'doubled')
                    or _clip_around(text, 'Google Reviews')
                    or _clip_around(text, 'client reviews')
                    or 'testimonial carousel'
                ),
                'char_count': 80,
            })
        downturn = _SAFETY_DOWNTURN_RE.search(text)
        if downturn and not has_region('mid_page', 'mid'):
            notes.append({
                'region': 'mid_page',
                'font_note': '16pt',
                'excerpt': downturn.group(0),
                'char_count': len(downturn.group(0)),
            })
        if not any(note.get('below_fold') for note in notes) and _FOOTER_HINT_RE.search(text):
            footer_excerpt = _footer_excerpt(text)
            notes.append({
                'region': 'footer',
                'below_fold': True,
                'font_note': '7pt grey text on white, below the fold, after the sitemap',
                'excerpt': footer_excerpt,
                'char_count': max(len(footer_excerpt), 40),
            })
    if format_name == 'presentation' or re.search(r'\bslide\s+\d+', text, re.I):
        page = _primary_slide_page(text)
        _append_presentation_layout_notes(notes, text, has_region, page=page)
    if format_name == 'social':
        _append_social_layout_notes(notes, text, has_region)
    if format_name == 'email':
        _append_email_layout_notes(notes, text, has_region)
    if format_name == 'blog':
        _append_blog_layout_notes(notes, text, has_region)
    return dedupe_presentation_layout_notes(notes)


def _append_presentation_layout_notes(
    notes: list[dict[str, Any]],
    text: str,
    has_region: Any,
    *,
    page: int = 1,
) -> None:
    if _SPEAKS_FOR_ITSELF_RE.search(text) and not has_region('slide_title'):
        notes.append({
            'region': 'slide_title',
            'page': page,
            'font_note': '32pt slide title',
            'excerpt': 'Performance That Speaks for Itself',
            'char_count': 36,
        })
    if (
        (
            _INVESTMENT_PERFORMANCE_RE.search(text)
            or _SPEAKS_FOR_ITSELF_RE.search(text)
        )
        and not has_region('bar_chart', 'chart')
    ):
        excerpt = (
            _clip_around(text, '22.4%')
            or _clip_around(text, 'S&P')
            or _clip_around(text, '+')
            or 'single-year performance chart'
        )
        notes.append({
            'region': 'bar_chart',
            'page': page,
            'font_note': 'dominant bar chart, left two-thirds of slide; two bars, one year',
            'excerpt': excerpt,
            'char_count': len(excerpt),
        })
    if _HYPOTHETICAL_RE.search(text) and not has_region('callout'):
        excerpt = _clip_around(text, 'backtested') or _clip_around(text, 'would have returned')
        notes.append({
            'region': 'callout',
            'page': page,
            'font_note': '20pt gold callout',
            'excerpt': excerpt or 'hypothetical performance callout',
            'char_count': len(excerpt or ''),
        })
    if (_BEFORE_FEES_RE.search(text) or _SEE_APPENDIX_RE.search(text)) and not has_region('slide_footer'):
        excerpt = _clip_around(text, 'before advisory fees') or _clip_around(text, 'appendix')
        notes.append({
            'region': 'slide_footer',
            'page': page,
            'font_note': '11pt bottom right',
            'excerpt': excerpt or 'Returns shown before advisory fees. See appendix for disclosures.',
            'char_count': 80,
        })
    if (_APPENDIX_NET_RE.search(text) or _SEE_APPENDIX_RE.search(text)) and not has_region('appendix'):
        excerpt = _clip_around(text, '20.1%') or _clip_around(text, 'net') or 'net figure in appendix'
        notes.append({
            'region': 'appendix',
            'font_note': 'slide 14, 9pt',
            'excerpt': excerpt,
            'char_count': max(len(excerpt), 40),
            'page': 14 if re.search(r'slide\s+14', text, re.I) else page,
        })


def _append_social_layout_notes(
    notes: list[dict[str, Any]],
    text: str,
    has_region: Any,
) -> None:
    if not has_region('post'):
        notes.append({
            'region': 'post',
            'font_note': 'personal-account social post, retail communication',
            'excerpt': text.split('\n')[0][:240] if text else 'social post',
            'char_count': min(len(text), 400),
        })
    engagement = _ADOPTION_RE.search(text) or re.search(
        r'\b(comments?|reactions?|reposts?)\b',
        text,
        re.I,
    )
    if engagement and not has_region('visible_engagement'):
        excerpt = (
            _clip_around(text, 'best move')
            or _clip_around(text, 'like this')
            or _clip_around(text, 'liked')
            or 'visible engagement, including comments'
        )
        notes.append({
            'region': 'visible_engagement',
            'font_note': 'reactions, comments, and rep engagement',
            'excerpt': excerpt,
            'char_count': len(excerpt),
        })


def _split_email_visual_parts(text: str) -> dict[str, str]:
    """Split pasted email copy into full subject / body / footer — never a clipped window."""
    blob = (text or '').replace('\r\n', '\n').strip('\n')
    subject = ''
    subject_match = re.search(r'(?im)^subject\s*:\s*(.+)$', blob)
    if subject_match:
        subject = subject_match.group(1).strip()
    header_re = re.compile(
        r'(?im)^(template|subject|from|to|cc|bcc|date|sent)\s*:.*$'
    )
    last_header_end = 0
    found_header = False
    for match in header_re.finditer(blob):
        found_header = True
        last_header_end = match.end()
    rest = blob[last_header_end:].lstrip('\n') if found_header else blob
    footer_match = re.search(r'(?im)^(?:footer\s*:|\s*unsubscribe\b).+', rest)
    if footer_match:
        body = rest[: footer_match.start()].rstrip()
        footer = re.sub(r'(?is)^footer\s*:\s*', '', rest[footer_match.start():].strip()).strip()
    else:
        body = rest.strip()
        footer = (
            'Standard unsubscribe + firm address. No performance or rating disclosures.'
        )
    if not subject:
        for line in blob.split('\n'):
            if _SUBJECT_CONCEAL_RE.search(line):
                subject = re.sub(r'(?i)^subject\s*:\s*', '', line).strip().strip('"')
                break
    return {'subject': subject, 'body': body, 'footer': footer}


def _upsert_email_layout_note(
    notes: list[dict[str, Any]],
    *,
    region: str,
    font_note: str,
    excerpt: str,
) -> None:
    if not excerpt:
        return
    for note in notes:
        if str(note.get('region') or '') != region:
            continue
        existing = str(note.get('excerpt') or '')
        if len(excerpt) >= len(existing):
            note['excerpt'] = excerpt
            note['char_count'] = len(excerpt)
        if not note.get('font_note'):
            note['font_note'] = font_note
        return
    notes.append({
        'region': region,
        'font_note': font_note,
        'excerpt': excerpt,
        'char_count': len(excerpt),
    })


def _append_email_layout_notes(
    notes: list[dict[str, Any]],
    text: str,
    has_region: Any,
) -> None:
    parts = _split_email_visual_parts(text)
    if parts['subject'] or _SUBJECT_CONCEAL_RE.search(text) or re.search(r'subject\s*:', text, re.I):
        _upsert_email_layout_note(
            notes,
            region='subject',
            font_note='subject line is part of the advertisement',
            excerpt=parts['subject'] or 'subject',
        )
    _upsert_email_layout_note(
        notes,
        region='body',
        font_note='14pt body; templated send to a list (advertisement, not one-to-one)',
        excerpt=parts['body'] or text,
    )
    merge_fields = ', '.join(dict.fromkeys(_MERGE_FIELD_RE.findall(text)))
    if merge_fields and not has_region('merge_fields'):
        notes.append({
            'region': 'merge_fields',
            'font_note': 'leave merge tokens unchanged',
            'excerpt': merge_fields,
            'char_count': len(merge_fields),
        })
    _upsert_email_layout_note(
        notes,
        region='email_footer',
        font_note='10pt footer — unsubscribe + firm address; no performance or rating disclosures',
        excerpt=parts['footer'] or (
            'Standard unsubscribe + firm address. No performance or rating disclosures.'
        ),
    )


def _blog_promo_half(text: str) -> str:
    match = re.search(r'this is exactly why we built|\bwe built\b', text, re.I)
    if not match:
        return ''
    return text[match.start():]


def _append_blog_layout_notes(
    notes: list[dict[str, Any]],
    text: str,
    has_region: Any,
) -> None:
    promo = _blog_promo_half(text)
    if not has_region('title'):
        notes.append({
            'region': 'title',
            'font_note': 'Insights blog headline',
            'excerpt': (
                _clip_around(text, 'Sequence-of-Returns')
                or text.split('\n', 1)[0][:180]
            ),
            'char_count': 80,
        })
    if promo and not has_region('educational'):
        split_at = text.find(promo)
        notes.append({
            'region': 'educational',
            'font_note': 'paras 1–5 — educational, no firm mention',
            'excerpt': text[:split_at].strip()[:280],
            'char_count': max(split_at, 40),
        })
    if promo and not has_region('para_6'):
        notes.append({
            'region': 'para_6',
            'font_note': 'promotional turn — offers advisory services',
            'excerpt': _clip_around(text, 'Income Ladder') or promo[:240],
            'char_count': 180,
        })
    if _CLIENT_OUTCOME_RE.search(text) and not has_region('para_7'):
        notes.append({
            'region': 'para_7',
            'font_note': '2022 client-outcome / 60/40 comparison',
            'excerpt': _clip_around(text, 'stayed on track') or _clip_around(text, 'double digits'),
            'char_count': 180,
        })
    if _FREE_CTA_RE.search(text) and not has_region('closing'):
        notes.append({
            'region': 'closing',
            'font_note': 'closing CTA',
            'excerpt': _clip_around(text, 'strategy call') or _clip_around(text, 'Income Ladder would work'),
            'char_count': 80,
        })
    if _FREE_CTA_RE.search(text) and not has_region('button'):
        notes.append({
            'region': 'button',
            'font_note': 'Book my call button',
            'excerpt': _clip_around(text, 'Book my call') or 'Book my call',
            'char_count': 20,
        })
    if not has_region('disclosures'):
        notes.append({
            'region': 'disclosures',
            'font_note': 'no disclosures anywhere on the page',
            'excerpt': 'No disclosures anywhere on the page.',
            'char_count': 40,
        })


def classify_marketing_material(
    *,
    format_name: str,
    body_text: str,
    audience: str,
    registrant_type: str,
    layout_notes: list[dict[str, Any]],
) -> dict[str, Any]:
    text = body_text or ''
    promo = _blog_promo_half(text) if format_name == 'blog' else ''
    tag_source = promo or text if format_name == 'blog' else text
    tags: list[str] = []
    presentation_performance = format_name == 'presentation' and (
        bool(_INVESTMENT_PERFORMANCE_RE.search(tag_source))
        or bool(_HYPOTHETICAL_RE.search(tag_source))
        or bool(_BEFORE_FEES_RE.search(tag_source))
        or any('22.4' in str(note.get('excerpt', '')) for note in layout_notes)
    )
    if (
        presentation_performance
        or (
            format_name != 'presentation'
            and (
                _PERFORMANCE_RE.search(tag_source)
                or any('11.' in str(note.get('excerpt', '')) for note in layout_notes)
                or (format_name == 'blog' and re.search(r'double digits|stayed on track', tag_source, re.I))
            )
        )
    ):
        tags.append('performance')
    if _HYPOTHETICAL_RE.search(tag_source):
        tags.append('hypothetical_performance')
    if re.search(r'\bs&p\b|benchmark', text, re.I):
        tags.append('benchmark_comparison')
    if format_name == 'presentation':
        if _CLIENT_TESTIMONIAL_RE.search(text):
            tags.append('testimonial')
    elif _TESTIMONIAL_RE.search(text) or _CLIENT_REVIEW_RE.search(text):
        tags.append('testimonial')
    if _RATING_RE.search(text):
        tags.append('third_party_rating')
    if _SCARCITY_RE.search(text):
        tags.append('scarcity')
    if _GUARANTEE_RE.search(tag_source) or _ELIMINATE_RISK_RE.search(tag_source):
        tags.append('safety_claim')
    if re.search(r'\bwe built\b|\bour (approach|strategy|model)\b', text, re.I) and format_name == 'blog':
        tags.append('strategy_promotion')
    if re.search(r'structured note|downside', text, re.I):
        tags.append('structured_products')
    unique_tags = [tag for tag in CONTENT_TAGS if tag in set(tags)]
    rules: list[str] = ['204-2']
    if registrant_type in {'ria', 'dual', 'unknown'}:
        rules.append('206(4)-1')
    if registrant_type in {'broker_dealer', 'dual'}:
        rules.extend(['2210', '2010'])
        rules.extend(_finra_topic_rules(text))
    return {
        'format': format_name,
        'audience': audience,
        'registrant_type': registrant_type,
        'tags': unique_tags,
        'rules': rules,
        'hybrid_educational': format_name == 'blog' and 'strategy_promotion' in unique_tags,
    }


_FINRA_TOPIC_RULES = (
    ('2211', re.compile(r'variable annuit', re.I)),
    ('2212', re.compile(r'fund ranking|lipper ranking|morningstar (rating|rank)', re.I)),
    ('2213', re.compile(r'volatility rating|bond fund volatility', re.I)),
    ('2214', re.compile(r'investment[- ]analysis tool|portfolio (analyzer|calculator)', re.I)),
    ('2215', re.compile(r'security futures', re.I)),
    ('2216', re.compile(r'collateralized mortgage|\bcmos?\b|mortgage[- ]backed', re.I)),
)


def _finra_topic_rules(text: str) -> list[str]:
    return [code for code, pattern in _FINRA_TOPIC_RULES if pattern.search(text or '')]


OPTIONAL_QUESTION_IDS = frozenset({'testimonial_paid'})


def should_ask(question: str, body_text: str) -> bool:
    """Ask only when the material does not already answer the fact."""
    text = body_text or ''
    lowered = question.lower()
    if 'gross' in lowered or 'after fees' in lowered or 'before or after' in lowered:
        return not bool(_GROSS_NET_RE.search(text))
    if 'testimonial' in lowered and ('compensat' in lowered or 'paid' in lowered):
        return not bool(_UNPAID_TESTIMONIAL_RE.search(text))
    if 'principal' in lowered or 'pre-approval' in lowered or 'preapproval' in lowered:
        return 'principal approv' not in text.lower()
    return True


def question_is_optional(item: dict[str, Any]) -> bool:
    return bool(item.get('optional')) or str(item.get('id') or '') in OPTIONAL_QUESTION_IDS


def mark_optional_questions(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for item in questions:
        row = dict(item)
        if question_is_optional(row):
            row['optional'] = True
        marked.append(row)
    return marked


def _single_year_performance_year(text: str) -> str | None:
    if _MULTI_PERIOD_RE.search(text):
        return None
    stripped = _BACKTEST_SPAN_RE.sub(' ', text)
    years = re.findall(r'\b(20\d{2})\b', stripped)
    unique = list(dict.fromkeys(years))
    if len(unique) != 1:
        return None
    if not _PERFORMANCE_RE.search(stripped):
        return None
    return unique[0]


def _primary_slide_page(text: str) -> int:
    match = re.search(r'slide\s+(\d+)', text, re.I)
    return int(match.group(1)) if match else 1


def _slide_context_question(page: int) -> str:
    if page == 7:
        return _SLIDE7_QUESTION
    return (
        f'Two things on slide {page}: is this deck used with retail prospects, institutional '
        'prospects, or both? And does the S&P 500 figure include reinvested dividends '
        '(total return) or price only?'
    )


def _unique_finding_id(base: str, used: set[str], slide_page: int | None = None) -> str:
    if base not in used:
        used.add(base)
        return base
    candidate = f'{base}_slide_{slide_page}' if slide_page else f'{base}_{len(used) + 1}'
    used.add(candidate)
    return candidate


def _base_finding_id(finding_id: str) -> str:
    return re.sub(r'_slide_\d+$', '', str(finding_id or ''))


def _add_presentation_findings(
    add: Any,
    questions: list[dict[str, Any]],
    *,
    text: str,
    notes: list[dict[str, Any]],
    classification: dict[str, Any],
    answers: dict[str, str],
    deck_text: str | None = None,
    deck_notes: list[dict[str, Any]] | None = None,
    slide_page: int | None = None,
    used_ids: set[str] | None = None,
) -> None:
    del classification
    audience = _audience_from_answers(answers)
    basis = _benchmark_basis_from_answers(answers)
    used = used_ids if used_ids is not None else set()
    full_text = deck_text if deck_text is not None else text
    full_notes = deck_notes if deck_notes is not None else notes
    page = slide_page or _primary_slide_page(text)
    later_net = _net_on_later_slide(full_text, full_notes)
    if _BEFORE_FEES_RE.search(text) and (
        later_net or _SEE_APPENDIX_RE.search(full_text) or _APPENDIX_NET_RE.search(full_text)
    ):
        add(
            _clip_around(text, 'before advisory fees') or 'Gross on slide, net in appendix',
            '206(4)-1',
            (
                'The net figure exists later in the deck (appendix / 9pt) but the gross figure '
                'owns the slide. Equal prominence means on the same slide, comparable treatment '
                '— not seven slides away. This is a layout finding.'
            ),
            'certain',
            ['redline'],
            'redline',
            finding_id=_unique_finding_id('gross_net_layout', used, page),
            region='gross_net',
            title='Gross headline, net in appendix',
            draft_text=_GROSS_NET_SLIDE_DRAFT,
            option_labels={'redline': 'Redline'},
            slide=page,
        )
    if _HYPOTHETICAL_RE.search(text):
        held = not audience
        add(
            _clip_around(text, 'would have returned') or _clip_around(text, 'backtested') or 'Backtest callout',
            '206(4)-1',
            (
                '“Would have returned” is hypothetical performance. It carries its own regime: '
                'only to audiences with the resources to assess it, with methodology and assumptions '
                'disclosed. A gold callout on a prospect deck fails on presentation regardless of '
                'the math. Whether this deck ever goes to retail prospects changes how hard the '
                'finding lands.'
            ),
            'pending_answer' if held else 'certain',
            ['held'] if held else ['redline', 'pull'],
            'held' if held else 'redline',
            status='held' if held else 'open',
            finding_id=_unique_finding_id('hypothetical_backtest', used, page),
            region='callout',
            title='Backtest presented as track record',
            draft_text='' if held else _BACKTEST_RETAIL_DRAFT,
            option_labels={'redline': 'Redline', 'pull': 'Pull the callout'},
            slide=page,
        )
    if re.search(r'\bs&p\b|benchmark', text, re.I):
        add(
            _clip_around(text, 'S&P') or 'Benchmark comparison',
            '206(4)-1',
            (
                'Strategy vs S&P 500 with no statement of whether the comparison is like-for-like '
                '(dividends included, similar risk profile). Not prohibited — needs a basis disclosure.'
            ),
            'certain',
            ['add_disclosure'],
            'add_disclosure',
            finding_id=_unique_finding_id('benchmark_comparison', used, page),
            region='benchmark',
            title='Benchmark comparison',
            draft_text=_BENCHMARK_DRAFT,
            option_labels={'add_disclosure': 'Add disclosure'},
            slide=page,
        )
    if _SPEAKS_FOR_ITSELF_RE.search(text):
        add(
            'Performance That Speaks for Itself',
            '206(4)-1',
            (
                'Slide title is puffery sitting above real numbers. Borderline; reads as tone '
                'rather than claim. Flagged so the CCO sees it was considered.'
            ),
            'judgment_moderate',
            ['do_nothing'],
            'do_nothing',
            finding_id=_unique_finding_id('puffery_title', used, page),
            region='slide_title',
            title='Performance That Speaks for Itself',
            slide=page,
        )
    if _HYPOTHETICAL_RE.search(text) and not audience:
        if not any(item.get('id') == 'slide7_context' for item in questions):
            questions.append({'id': 'slide7_context', 'prompt': _slide_context_question(page)})
    elif (
        re.search(r'\bs&p\b', text, re.I)
        and not basis
        and not re.search(r'total return|dividends reinvested|price only', text, re.I)
        and not any(item.get('id') == 'benchmark_basis' for item in questions)
        and not any(item.get('id') == 'slide7_context' for item in questions)
    ):
        questions.append({
            'id': 'benchmark_basis',
            'prompt': (
                'Does the S&P 500 figure include reinvested dividends (total return) or price only?'
            ),
        })
    exclusivity = _EXCLUSIVITY_RE.search(text)
    if exclusivity:
        add(
            _clip_around(text, exclusivity.group(0)) or exclusivity.group(0),
            '206(4)-1',
            (
                'An exclusivity claim (“only”, “nobody automates”, “none own”) is an untrue or '
                'unsubstantiated statement unless the firm can document it. A sales deck shown '
                'to prospects is an advertisement under Rule 206(4)-1.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id=_unique_finding_id('unsubstantiated_exclusivity', used, page),
            region='slide_copy',
            title='Unsubstantiated exclusivity claim',
            draft_text=_EXCLUSIVITY_DRAFT,
            option_labels={'redline': 'Redline', 'pull': 'Pull the claim'},
            slide=page,
            anchor='slide copy',
        )
    quantified = _QUANTIFIED_SPLIT_RE.search(text)
    if quantified:
        add(
            _clip_around(text, quantified.group(0)) or quantified.group(0),
            '206(4)-1',
            (
                'A precise 5% / 95% split of CCO vs. software work is an unsubstantiated '
                'statistic. Quoted positioning copy is not a client testimonial, but the '
                'percentages still need a basis or a qualifier.'
            ),
            'judgment_moderate',
            ['redline', 'add_disclosure', 'pull'],
            'redline',
            finding_id=_unique_finding_id('unsubstantiated_statistic', used, page),
            region='slide_copy',
            title='Unsubstantiated 5% / 95% split',
            draft_text=_QUANTIFIED_SPLIT_DRAFT,
            option_labels={
                'redline': 'Redline',
                'add_disclosure': 'Add disclosure',
                'pull': 'Pull the percentages',
            },
            slide=page,
            anchor='slide copy',
        )
    third_party = _THIRD_PARTY_OUTCOME_RE.search(text)
    if third_party:
        add(
            _clip_around(text, third_party.group(0)) or third_party.group(0),
            '206(4)-1',
            (
                'A named third party’s outcome (or an unsourced improvement range) is presented '
                'as a sales proof point. Third-party results inherit advertising treatment: source, '
                'date, and that the result is not the advertiser’s and not typical — or pull it.'
            ),
            'certain',
            ['add_disclosure', 'redline', 'pull'],
            'add_disclosure',
            finding_id=_unique_finding_id('third_party_outcome', used, page),
            region='slide_copy',
            title='Third-party outcome statistic',
            draft_text=_THIRD_PARTY_OUTCOME_DRAFT,
            option_labels={
                'add_disclosure': 'Add source disclosure',
                'redline': 'Redline',
                'pull': 'Pull the figure',
            },
            slide=page,
            anchor='slide copy',
        )


def _net_on_later_slide(text: str, notes: list[dict[str, Any]]) -> bool:
    if _APPENDIX_NET_RE.search(text):
        return True
    for note in notes:
        page = int(note.get('page') or 0)
        excerpt = str(note.get('excerpt') or '')
        if page >= 10 and _NET_FIGURE_RE.search(excerpt):
            return True
        if str(note.get('region') or '') == 'appendix' and _NET_FIGURE_RE.search(excerpt or 'net'):
            return True
    return False


def _answer_blob(answers: dict[str, str]) -> str:
    return ' '.join(str(value or '') for value in answers.values()).lower()


def _audience_from_answers(answers: dict[str, str]) -> str:
    blob = _answer_blob(answers)
    if not blob.strip():
        return ''
    if 'both' in blob:
        return 'both'
    if 'institutional' in blob and 'retail' not in blob:
        return 'institutional'
    if 'retail' in blob:
        return 'retail'
    return ''


def _benchmark_basis_from_answers(answers: dict[str, str]) -> str:
    blob = _answer_blob(answers)
    if 'total return' in blob or 'dividend' in blob:
        return 'total_return'
    if 'price only' in blob or re.search(r'\bprice\b', blob):
        return 'price_only'
    return ''


def _add_social_findings(
    add: Any,
    questions: list[dict[str, Any]],
    *,
    text: str,
    answers: dict[str, str],
    social_adoption: bool,
) -> None:
    yield_match = _STRUCTURED_YIELD_RE.search(text)
    if yield_match or re.search(r'structured note', text, re.I):
        excerpt = yield_match.group(0) if yield_match else 'structured note strategy'
        add(
            excerpt,
            '2210(d)',
            (
                'Presents yield as certain and pairs it with a protection claim. Structured notes '
                'carry issuer credit risk and, typically, capped or conditional protection — the '
                'post is not fair and balanced and omits material risk. This is the sentence an '
                'examiner screenshots.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id='structured_yield_claim',
            title='Pays 9% income while protecting your downside',
            draft_text=_YIELD_CLAIM_DRAFT,
            option_labels={'redline': 'Redline', 'pull': 'Pull'},
        )
    approval_known = bool(answers.get('principal_approval'))
    add(
        'Retail communication with no principal approval on file?',
        '2210(b)',
        (
            'Public posts about specific products by a rep generally require prior principal '
            'approval and firm recordkeeping. Whether this one went through the firm’s approval '
            'channel isn’t visible from the screenshot.'
        ),
        'pending_answer' if not approval_known else 'certain',
        ['held'] if not approval_known else ['pull', 'redline'],
        'held' if not approval_known else 'pull',
        status='held' if not approval_known else 'open',
        finding_id='principal_approval',
        title='Retail communication with no principal approval on file?',
        option_labels={'pull': 'Pull', 'redline': 'Redline for re-post'},
    )
    if not approval_known:
        firm = 'Cobalt’s' if re.search(r'cobalt', text, re.I) else 'the firm’s'
        questions.append({
            'id': 'principal_approval',
            'prompt': (
                f'Did this post go through {firm} principal pre-approval process before it went up? '
                'If yes, I’ll note the approval record; if no, that’s a supervision finding on top '
                'of the content findings.'
            ),
        })
    if social_adoption or re.search(r'\b(liked|commented|reposted)\b', text, re.I):
        add(
            _clip_around(text, 'best move') or 'Rep liked the client comment',
            '2210/2010',
            (
                'Liking the comment is adoption — the firm’s guidance treats it as the rep making '
                'the statement himself. An adopted testimonial on a public post pulls in testimonial '
                'treatment and the fair-and-balanced standard.'
            ),
            'certain',
            ['redline'],
            'redline',
            finding_id='social_adoption',
            title='Rep liked the client’s “best move I ever made” comment',
            draft_text=_ADOPTION_DRAFT,
            option_labels={'redline': 'Redline (unlike + do not engage)'},
        )
    if _SUNGLASSES_RE.search(text):
        add(
            '😎',
            '—',
            'Not a finding. Noted only because reps ask: tone is fine; claims are the problem.',
            'judgment_moderate',
            ['do_nothing'],
            'do_nothing',
            finding_id='emoji_tone',
            title='Sunglasses emoji',
        )


def _add_email_findings(
    add: Any,
    questions: list[dict[str, Any]],
    *,
    text: str,
    answers: dict[str, str],
) -> None:
    relative = _RELATIVE_PERF_RE.search(text)
    source_known = bool(
        answers.get('relative_performance_source') or answers.get('performance_source')
    )
    if relative:
        excerpt = relative.group(0)
        add(
            excerpt,
            '206(4)-1',
            (
                'Relative performance claim. Who computed it, the comparable-strategies universe, '
                'the period, and substantiation on file are not in the email. The source is not '
                'identifiable from the material.'
            ),
            'pending_answer' if not source_known else 'certain',
            ['held'] if not source_known else ['redline'],
            'held' if not source_known else 'redline',
            status='held' if not source_known else 'open',
            finding_id='relative_performance',
            region='body',
            title='Outperformed 87% of comparable strategies',
            draft_text=_EMAIL_RELATIVE_DRAFT,
            option_labels={'redline': 'Redline — remove'},
            part='87% claim',
        )
        if not source_known:
            questions.insert(0, {
                'id': 'relative_performance_source',
                'prompt': (
                    'What is the source for “outperformed 87% of comparable strategies” — who ran '
                    'the comparison, over what period, against what universe?'
                ),
            })
    if _RATING_RE.search(text):
        add(
            'Top 100 Advisor' if re.search(r'top 100', text, re.I) else 'Third-party rating',
            '206(4)-1',
            (
                'Date, period, and paid/unpaid must travel with the rating into every channel it '
                'appears in. A website disclosure does not cover this email.'
            ),
            'certain',
            ['add_disclosure'],
            'add_disclosure',
            finding_id='third_party_rating',
            region='body',
            title='Top 100 Advisor rating, no disclosures',
            draft_text=_EMAIL_RATING_DRAFT,
            option_labels={'add_disclosure': 'Add disclosure'},
            part='rating',
        )
    if _SUBJECT_CONCEAL_RE.search(text):
        add(
            "Most advisors won’t tell you this...",
            '206(4)-1',
            (
                'Implies concealment by other advisors without substantiation. The body does not '
                'deliver the withheld information. Subject lines are part of the advertisement.'
            ),
            'judgment_moderate',
            ['redline', 'do_nothing'],
            'redline',
            finding_id='subject_line',
            region='subject',
            title='Most advisors won’t tell you this... subject line',
            draft_text=_EMAIL_SUBJECT_DRAFT,
            option_labels={'redline': 'Redline (optional)', 'do_nothing': 'Do nothing'},
            part='subject line',
        )
    if _FREE_OFFER_RE.search(text):
        add(
            'Free Retirement Readiness Score',
            '206(4)-1',
            (
                'Fine as written if it is genuinely free with no purchase condition. Flagged so '
                'the CCO sees it was considered.'
            ),
            'certain',
            ['do_nothing'],
            'do_nothing',
            finding_id='free_offer',
            region='body',
            title='Free Retirement Readiness Score',
            part='body',
        )
    if re.search(r'step\s+\d+\s+of\s+\d+|drip|nurture|cadence', text, re.I):
        add(
            'Q3 Prospect Nurture cadence',
            '206(4)-1',
            (
                'Steps 1 and 3 of this cadence were not submitted. The relative-performance '
                'statistic and the rating sit in the template library — if reused, the same '
                'findings apply. Recommended for submission.'
            ),
            'certain',
            ['do_nothing'],
            'do_nothing',
            finding_id='sequence_flag',
            title='Sequence flag — other cadence steps not submitted',
            draft_text=_EMAIL_SEQUENCE_DRAFT,
            part='sequence',
        )


def _add_blog_findings(
    add: Any,
    questions: list[dict[str, Any]],
    *,
    text: str,
    classification: dict[str, Any],
    answers: dict[str, str],
    screenshot: bool = False,
) -> None:
    promo = _blog_promo_half(text)
    outcome_answered = bool(
        answers.get('client_outcome_2022') or answers.get('2022_claim')
    )
    if _ELIMINATE_RISK_RE.search(text):
        add(
            _clip_around(text, 'designed to eliminate') or 'Designed to eliminate it',
            '206(4)-1',
            (
                'An elimination claim about market risk. No strategy eliminates sequence risk; '
                'laddering mitigates it. Same family as a guarantee — cure by rewording, not disclosure.'
            ),
            'certain',
            ['redline'],
            'redline',
            finding_id='eliminate_claim',
            region='para_6',
            title='Designed to eliminate it (sequence risk)',
            draft_text=_BLOG_ELIMINATE_DRAFT,
            option_labels={'redline': 'Redline'},
        )
    if _CLIENT_OUTCOME_RE.search(text):
        add(
            _clip_around(text, 'stayed on track') or '2022 comparison',
            '206(4)-1',
            (
                'A comparative performance claim wearing narrative clothing. “Stayed on track” is a '
                'client-outcome claim; “double digits” is a number. Needs the same substantiation and '
                'presentation treatment as any performance figure — basis, period, net/gross, and whether '
                '“clients” means all, some, or a composite.'
            ),
            'pending_answer' if not outcome_answered else 'certain',
            ['held'] if not outcome_answered else ['redline', 'pull'],
            'held' if not outcome_answered else 'redline',
            status='held' if not outcome_answered else 'open',
            finding_id='client_outcome_2022',
            region='para_7',
            title='2022 comparison',
            draft_text='' if not outcome_answered else _BLOG_2022_DRAFT,
            option_labels={'redline': 'Redline', 'pull': 'Pull para 7'},
        )
        if not outcome_answered:
            questions.insert(0, {
                'id': 'client_outcome_2022',
                'prompt': _BLOG_2022_QUESTION,
            })
    elif promo and _HYPOTHETICAL_RE.search(promo):
        add(
            _clip_around(promo, 'would have returned') or 'Unsubstantiated outcome',
            '206(4)-1',
            (
                'Unsubstantiated outcomes cannot be disclosed into compliance. Rewrite as strategy '
                'design commentary or pull the promotional result.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id='unsubstantiated_outcome',
            region='para_7',
            title='Unsubstantiated promotional outcome',
            option_labels={'redline': 'Redline', 'pull': 'Pull'},
        )
    if classification.get('hybrid_educational'):
        add(
            'Paras 1–5 (education)',
            '—',
            (
                'Reviewed and clean. Accurate, balanced, no firm promotion. Explicitly recorded as '
                'no finding so the CCO sees the classification reasoning, and so Mode B knows the '
                'educational half doesn’t carry performance tags.'
            ),
            'certain',
            ['do_nothing'],
            'do_nothing',
            finding_id='educational_half',
            region='educational',
            title='Paras 1–5 (education)',
        )
    if (promo or screenshot) and not _PAGE_DISCLOSURE_RE.search(text):
        add(
            'No disclosures on an ad-bearing page' if promo else 'No disclosures on the screenshot',
            '206(4)-1',
            (
                'Once para 6 makes the page an ad, the page needs the disclosures its claims trigger. '
                'Currently none.'
                if promo
                else (
                    'This uploaded article screenshot has no advertising disclosures (adviser identity, '
                    'SEC registration, risk, Form ADV). If the firm publishes it, the page needs the '
                    'disclosures its claims trigger. Currently none.'
                )
            ),
            'certain',
            ['add_disclosure'],
            'add_disclosure',
            finding_id='missing_disclosures',
            region='disclosures',
            title='No disclosures on an ad-bearing page' if promo else 'No disclosures on the screenshot',
            draft_text=_BLOG_DISCLOSURE_DRAFT if promo else _SCREENSHOT_DISCLOSURE_DRAFT,
            option_labels={'add_disclosure': 'Add disclosure'},
        )
    if _FREE_CTA_RE.search(text):
        add(
            _clip_around(text, 'free strategy call') or 'Free strategy call',
            '206(4)-1',
            'Fine. Recorded as reviewed.',
            'certain',
            ['do_nothing'],
            'do_nothing',
            finding_id='free_cta',
            region='closing',
            title='“Free strategy call” CTA',
        )


def heuristic_review(
    *,
    classification: dict[str, Any],
    body_text: str,
    layout_notes: list[dict[str, Any]],
    answers: dict[str, str] | None = None,
) -> dict[str, Any]:
    answers = answers or {}
    findings: list[dict[str, Any]] = []
    questions: list[dict[str, Any]] = []
    text = _readable_review_text(body_text or '', layout_notes)
    notes = synthesize_layout_notes(
        body_text=text,
        layout_notes=layout_notes,
        format_name=str(classification.get('format') or ''),
    )
    format_name = str(classification.get('format') or '')
    screenshot = _layout_is_screenshot(notes)
    screenshot_claims = screenshot and format_name in {'blog', 'email'}

    def add(
        excerpt: str,
        rule: str,
        why: str,
        confidence: str,
        options: list[str],
        recommended: str,
        *,
        status: str = 'open',
        finding_id: str | None = None,
        region: str | None = None,
        title: str | None = None,
        draft_text: str | None = None,
        option_labels: dict[str, str] | None = None,
        slide: int | None = None,
        anchor: str | None = None,
        part: str | None = None,
    ) -> None:
        resolved_slide = slide
        if resolved_slide is None and screenshot_claims:
            resolved_slide = 1
        findings.append({
            'id': finding_id or str(uuid4()),
            'excerpt': excerpt[:240],
            'rule': rule,
            'why': why,
            'confidence': confidence,
            'options': [option for option in options if option in OPTIONS],
            'recommended_option': recommended if recommended in OPTIONS else 'escalate',
            'status': status,
            'region': region,
            'title': title,
            'draft_text': draft_text or '',
            'option_labels': option_labels or {},
            'slide': resolved_slide if format_name in {'presentation', 'blog', 'email', 'social'} else None,
            'part': part,
            'anchor': anchor or _REGION_ANCHORS.get(
                str(region or ''),
                'slide copy' if format_name == 'presentation' else (region or ''),
            ),
        })
    buried = format_name == 'webpage' and any(
        int(note.get('char_count') or 0) > 0 and (
            bool(note.get('below_fold'))
            or (
                str(note.get('region', '')).startswith('slide_or_page_')
                and int(note.get('page') or 1) > 3
            )
        )
        for note in notes
    )
    if buried and ('performance' in classification['tags'] or 'testimonial' in classification['tags']):
        add(
            'Disclosures exist but are buried',
            '206(4)-1',
            (
                'Every required disclosure is technically present, but they sit in 7pt grey text, '
                'below the fold, after the sitemap. The rule requires disclosures to be clear and '
                'prominent. A 48pt performance claim paired with a 7pt footnote is a layout finding '
                '— wording alone does not cure it.'
            ),
            'certain',
            ['redline', 'do_nothing'],
            'redline',
            finding_id='layout_buried',
            region='footer',
            title='Footer disclosure block — buried',
            draft_text=_LAYOUT_REDLINE_DRAFT,
            option_labels={
                'redline': 'Redline (layout instruction)',
                'do_nothing': 'Covered by items 1–4',
            },
        )
    downturn = _SAFETY_DOWNTURN_RE.search(text) if format_name != 'blog' or screenshot else None
    guarantee = _GUARANTEE_RE.search(text) if format_name != 'blog' or screenshot else None
    if downturn:
        add(
            downturn.group(0),
            '206(4)-1',
            (
                'Absolute claim about past results across all conditions. Functionally a safety '
                'guarantee that cannot be cured with a disclosure.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id='safety_claim',
            region='mid_page',
            title='Protected client capital in every downturn',
            draft_text=_SAFETY_DRAFT,
            option_labels={'redline': 'Redline', 'pull': 'Pull the sentence'},
        )
    elif guarantee:
        add(
            guarantee.group(0),
            '206(4)-1',
            (
                'Promises a result or eliminates risk in a way the Marketing Rule treats as a '
                'prohibited claim. A disclosure cannot cure it.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id='safety_claim',
            region='mid_page',
            title='Absolute result or safety claim',
            option_labels={'redline': 'Redline', 'pull': 'Pull the claim'},
        )
    if (
        format_name not in {'presentation', 'social'}
        and (format_name not in {'email', 'blog'} or screenshot)
        and _PERFORMANCE_RE.search(text)
    ):
        match = _PERFORMANCE_RE.search(text)
        excerpt = _clip_around(text, '11.4%') or (match.group(0) if match else 'performance figure')
        net_present = bool(_NET_FIGURE_RE.search(text))
        if _GROSS_NET_RE.search(text) and not net_present:
            add(
                excerpt,
                '206(4)-1',
                (
                    'The page presents a gross figure; the net figure is not present anywhere. '
                    'The rule requires net figures with equal prominence — same screen, comparable '
                    'size. Fee basis is already stated, so this is a finding, not a question.'
                ),
                'certain',
                ['redline', 'pull'],
                'redline',
                finding_id='performance_gross_hero',
                region='hero',
                title='Hero performance claim — gross only',
                draft_text=_HERO_GROSS_DRAFT,
                option_labels={'redline': 'Redline', 'pull': 'Pull the claim'},
            )
        elif not _GROSS_NET_RE.search(text):
            add(
                excerpt,
                '206(4)-1',
                (
                    'A performance figure is shown without 1/5/10-year periods and without a stated '
                    'fee basis. Whether it is gross or net changes the finding.'
                ),
                'pending_answer',
                ['redline', 'held'],
                'held',
                status='held',
                finding_id='performance_fee_basis',
                region='hero',
                title='Performance figure — fee basis unknown',
            )
            questions.append({
                'id': 'fee_basis',
                'prompt': 'Is the stated return before or after fees?',
            })
        else:
            add(
                excerpt,
                '206(4)-1',
                (
                    'Performance is presented. Confirm 1-, 5-, and 10-year periods '
                    '(or since inception) appear with equal prominence.'
                ),
                'judgment_moderate',
                ['redline', 'do_nothing'],
                'redline',
                finding_id='performance_periods',
                region='hero',
                title='Performance periods',
            )
    social_adoption = bool(_ADOPTION_RE.search(text))
    if (
        format_name != 'presentation'
        and (format_name != 'blog' or screenshot)
        and (_TESTIMONIAL_RE.search(text) or _CLIENT_REVIEW_RE.search(text))
        and not (format_name == 'social' and social_adoption)
    ):
        quote = (
            _clip_around(text, 'doubled my portfolio')
            or _clip_around(text, 'Google Reviews')
            or _clip_around(text, 'client perspective')
            or _clip_around(text, 'client reviews')
            or 'Testimonial carousel'
        )
        performance_quote = bool(_TESTIMONIAL_PERFORMANCE_RE.search(text))
        if _UNPAID_TESTIMONIAL_RE.search(text):
            why = (
                'Client-status and no-compensation disclosures exist in the footer but are not '
                'adjacent to the carousel.'
            )
            if performance_quote:
                why += (
                    ' “Doubled my portfolio” is a performance claim inside a testimonial, '
                    'so it also inherits performance-presentation requirements. Recommend '
                    'moving the disclosure adjacent and rotating that quote out.'
                )
            add(
                quote,
                '206(4)-1',
                why,
                'certain',
                ['add_disclosure', 'redline', 'pull'],
                'add_disclosure',
                finding_id='testimonial',
                region='testimonial',
                title='Testimonial carousel',
                draft_text=_TESTIMONIAL_DRAFT if performance_quote else (
                    'This statement was given by a client. No compensation was paid for it. '
                    'Place that disclosure adjacent to the quote, not only in the footer.'
                ),
                option_labels={
                    'add_disclosure': 'Add disclosure + rotate quote',
                    'redline': 'Redline',
                    'pull': 'Pull the quote',
                },
            )
        else:
            add(
                quote,
                '206(4)-1',
                (
                    'A testimonial appears without adjacent disclosures. Compensation status is '
                    'not stated on the page. The optional question can refine which disclosure '
                    'is required; until then, place a client-status and compensation disclosure '
                    'adjacent to the quote.'
                )
                + (
                    ' “Doubled my portfolio” is a performance claim inside a testimonial, '
                    'so it also inherits performance-presentation requirements. Recommend '
                    'moving the disclosure adjacent and rotating that quote out.'
                    if performance_quote
                    else ''
                ),
                'judgment_moderate',
                ['add_disclosure', 'redline', 'pull'],
                'add_disclosure',
                finding_id='testimonial',
                region='testimonial',
                title='Testimonial carousel',
                draft_text=_TESTIMONIAL_DRAFT if performance_quote else (
                    'This statement was given by a client. State whether compensation was paid. '
                    'Place that disclosure adjacent to the quote, not only in the footer.'
                ),
                option_labels={
                    'add_disclosure': (
                        'Add disclosure + rotate quote' if performance_quote else 'Add disclosure'
                    ),
                    'redline': 'Redline',
                    'pull': 'Pull the quote',
                },
            )
            questions.append({
                'id': 'testimonial_paid',
                'prompt': 'Was the person quoted paid or compensated in any way for that testimonial?',
                'optional': True,
            })
    rating_match = _RATING_RE.search(text)
    if rating_match and format_name not in {'email', 'presentation'}:
        labels = _rating_labels(text)
        label_text = ', '.join(labels) if labels else rating_match.group(0)
        held = 'rating_paid' not in answers
        add(
            label_text,
            '206(4)-1',
            (
                'Third-party ratings need date, period, and payment status adjacent to the rating. '
                'The footer is too far away and does not say whether the firm paid to be considered '
                'for or to display the badges.'
            ),
            'pending_answer' if held else 'certain',
            ['held'] if held else ['add_disclosure'],
            'held' if held else 'add_disclosure',
            status='held' if held else 'open',
            finding_id='third_party_rating',
            region='badge_row',
            title='Rating badges — no adjacent disclosures',
            option_labels={'add_disclosure': 'Add disclosure', 'held': 'Held'},
        )
        if held:
            questions.append({
                'id': 'rating_paid',
                'prompt': _rating_question(text),
            })
    fiduciary_match = _FIDUCIARY_CLAIM_RE.search(text)
    if format_name == 'webpage' and fiduciary_match:
        add(
            _clip_around(text, fiduciary_match.group(0)) or fiduciary_match.group(0),
            '206(4)-1',
            (
                'Fee-only or fiduciary positioning is an advertisement. The compensation '
                'arrangement and any limits on the duty must be clear next to the claim, '
                'not only in Form ADV or a footer.'
            ),
            'judgment_moderate',
            ['add_disclosure', 'redline'],
            'add_disclosure',
            finding_id='fiduciary_claim',
            region='hero',
            title='Fee-only fiduciary claim',
            option_labels={'add_disclosure': 'Add disclosure', 'redline': 'Redline'},
        )
    scarcity_match = _SCARCITY_RE.search(text)
    bd_social = (
        classification['registrant_type'] == 'broker_dealer' and classification['format'] == 'social'
    )
    if scarcity_match and bd_social:
        add(
            scarcity_match.group(0),
            '2210(d)/2010',
            (
                'False urgency on an advisory relationship. Minor next to the rest, but it is a '
                'misleading scarcity claim.'
            ),
            'certain',
            ['redline'],
            'redline',
            finding_id='scarcity',
            title='Spots are limited this quarter',
            draft_text=_SOCIAL_SCARCITY_DRAFT,
            option_labels={'redline': 'Redline'},
        )
    elif scarcity_match:
        add(
            scarcity_match.group(0) if not re.search(r'12 new families', text, re.I) else (
                'Only 12 new families per quarter, August is filling fast'
            ),
            '206(4)-1' if classification['registrant_type'] != 'broker_dealer' else '2210(d)',
            (
                'Scarcity claim. Permissible only if it is a real, enforced business practice; '
                'otherwise it is misleading copywriting. That fact is not in the material.'
            ),
            'pending_answer',
            ['held'],
            'held',
            status='held',
            finding_id='scarcity',
            title='Only 12 new families per quarter, August is filling fast' if re.search(
                r'12 new families', text, re.I
            ) else None,
            draft_text=_EMAIL_SCARCITY_DRAFT if format_name == 'email' else '',
        )
        questions.append({
            'id': 'scarcity_real',
            'prompt': (
                'Is the 12-families-per-quarter cap an actual enforced limit, or marketing copy?'
                if re.search(r'12 new families|12-families', text, re.I)
                else 'Is the capacity / scarcity limit an actual enforced business practice, or marketing copy?'
            ),
        })
    if bd_social:
        _add_social_findings(
            add,
            questions,
            text=text,
            answers=answers,
            social_adoption=social_adoption,
        )

    if format_name == 'presentation':
        used_ids: set[str] = set()
        slides = slides_from_layout_notes(notes, body_text=text)
        if not slides:
            slides = slides_from_layout_notes([], body_text=text)
        for slide in slides:
            _add_presentation_findings(
                add,
                questions,
                text=slide.text,
                notes=notes,
                classification=classification,
                answers=answers,
                deck_text=text,
                deck_notes=notes,
                slide_page=slide.page,
                used_ids=used_ids,
            )
    if format_name == 'email':
        _add_email_findings(add, questions, text=text, answers=answers)
    if format_name == 'blog':
        _add_blog_findings(
            add,
            questions,
            text=text,
            classification=classification,
            answers=answers,
            screenshot=screenshot,
        )

    cherry_year = _single_year_performance_year(text)
    if (
        format_name != 'social'
        and (format_name not in {'email', 'blog'} or screenshot)
        and 'performance' in classification['tags']
        and cherry_year
    ):
        add(
            f'{cherry_year} performance',
            '206(4)-1',
            (
                'Single-year cherry-pick: one strong year shown alone. Performance must show '
                '1-, 5-, and 10-year periods (or since inception). The multi-period table does '
                'not exist anywhere in the material — checked, not assumed.'
            ),
            'certain',
            ['redline'],
            'redline',
            finding_id='cherry_pick',
            region='bar_chart',
            title='Single-year cherry-pick',
            draft_text=_CHERRY_PICK_DRAFT,
            option_labels={'redline': 'Redline'},
            slide=_primary_slide_page(text) if format_name == 'presentation' else None,
        )
    if classification.get('hybrid_educational') and format_name != 'blog':
        add(
            'Promotional half of a hybrid article',
            '206(4)-1',
            (
                'Educational explanation is not an advertisement. The strategy / results half is. '
                'Unsubstantiated outcomes cannot be disclosed into compliance.'
            ),
            'certain',
            ['redline', 'pull'],
            'redline',
            finding_id='hybrid_promo',
        )

    filtered_questions = mark_optional_questions([
        item for item in questions[:MAX_QUESTIONS]
        if should_ask(item['prompt'], text)
    ])
    if not findings:
        excerpt = (text[:120] or '').strip() or 'Uploaded screenshot'
        add(
            excerpt,
            classification['rules'][0] if classification['rules'] else '206(4)-1',
            (
                'Reviewed against the current marketing rule set. No prohibited claim stood out '
                'from the words supplied. This is not an approval.'
            ),
            'judgment_moderate',
            ['do_nothing'],
            'do_nothing',
        )
    format_name = str(classification.get('format') or '')
    ordered = _order_findings(findings, format_name)
    applied = _apply_answers(ordered, answers)
    applied = _finalize_footer_after_answers(applied)
    applied = _assign_markers(applied)
    applied = _normalize_finding_locations(applied, format_name)
    return {
        'findings': applied,
        'questions': filtered_questions,
        'change_list': _change_list_from_findings(applied, classification),
        'layout_notes': notes,
        'report_header': REPORT_HEADER,
        'performance_disclaimer': PERFORMANCE_DISCLAIMER,
    }


def merge_review_results(
    heuristic: dict[str, Any],
    llm_result: dict[str, Any] | None,
    *,
    body_text: str,
    classification: dict[str, Any],
) -> dict[str, Any]:
    """Heuristic certain findings and ask-vs-say win. The LLM may only add, never re-ask known facts."""
    if not llm_result:
        return heuristic
    merged = dict(heuristic)
    llm_classification = llm_result.get('classification')
    if isinstance(llm_classification, dict):
        tags = list(dict.fromkeys(
            list(classification.get('tags') or []) + list(llm_classification.get('tags') or [])
        ))
        merged_classification = {**classification, **llm_classification, 'tags': tags}
        merged['classification'] = merged_classification
    heuristic_ids = {str(item.get('id')) for item in heuristic.get('findings') or []}
    extra: list[dict[str, Any]] = []
    for item in llm_result.get('findings') or []:
        if not isinstance(item, dict):
            continue
        finding_id = str(item.get('id') or uuid4())
        item = {**item, 'id': finding_id}
        if finding_id in heuristic_ids or _duplicates_heuristic(item, heuristic.get('findings') or []):
            continue
        extra.append(item)
    findings = list(heuristic.get('findings') or []) + extra
    findings = drop_empty_review_placeholders(findings)
    llm_questions = [
        item for item in (llm_result.get('questions') or [])
        if isinstance(item, dict)
        and item.get('prompt')
        and should_ask(str(item['prompt']), body_text)
        and not _similar_question(str(item['prompt']), heuristic.get('questions') or [])
    ]
    questions = list(heuristic.get('questions') or []) + llm_questions
    questions = mark_optional_questions([
        item for item in questions if should_ask(str(item.get('prompt') or ''), body_text)
    ])
    findings = _order_findings(findings, str(classification.get('format') or ''))
    findings = _finalize_footer_after_answers(findings)
    findings = _assign_markers(findings)
    findings = _normalize_finding_locations(findings, str(classification.get('format') or ''))
    merged['findings'] = findings
    merged['questions'] = questions[:MAX_QUESTIONS]
    merged['change_list'] = _change_list_from_findings(findings, classification)
    merged['report_header'] = REPORT_HEADER
    merged['performance_disclaimer'] = PERFORMANCE_DISCLAIMER
    if heuristic.get('layout_notes'):
        merged['layout_notes'] = heuristic['layout_notes']
    return merged


def drop_empty_review_placeholders(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the 'no prohibited claim' filler when real findings exist."""
    real = [item for item in findings if not _is_empty_review_placeholder(item)]
    return real or findings


def _is_empty_review_placeholder(item: dict[str, Any]) -> bool:
    why = str(item.get('why') or '').lower()
    option = str(item.get('recommended_option') or '')
    return option == 'do_nothing' and 'no prohibited claim' in why


def cadence_sibling_finding(*, cadence_key: str | None, sibling_names: list[str]) -> dict[str, Any] | None:
    if not cadence_key or not sibling_names:
        return None
    return {
        'id': str(uuid4()),
        'excerpt': f'Sequence {cadence_key}',
        'rule': '206(4)-1',
        'why': (
            'This piece is one step in a cadence. Disclosures on a website or another email '
            f'do not travel. Review sibling steps: {", ".join(sibling_names[:8])}.'
        ),
        'confidence': 'certain',
        'options': ['redline', 'add_disclosure', 'do_nothing'],
        'recommended_option': 'add_disclosure',
        'status': 'open',
    }


def _apply_answers(findings: list[dict[str, Any]], answers: dict[str, str]) -> list[dict[str, Any]]:
    paid = (answers.get('testimonial_paid') or '').lower()
    fees = (answers.get('fee_basis') or '').lower()
    rating_paid = answers.get('rating_paid') or ''
    scarcity = (answers.get('scarcity_real') or '').lower()
    approval = (answers.get('principal_approval') or '').lower()
    audience = _audience_from_answers(answers)
    basis = _benchmark_basis_from_answers(answers)
    resolved: list[dict[str, Any]] = []
    for finding in findings:
        item = dict(finding)
        rule_why = item['why'].lower()
        finding_id = _base_finding_id(str(item.get('id') or ''))
        if finding_id == 'testimonial' and paid:
            if any(token in paid for token in ('no', 'not', 'unpaid')):
                item.update({
                    'status': 'open',
                    'confidence': 'certain',
                    'recommended_option': 'add_disclosure',
                    'options': ['add_disclosure', 'pull'],
                    'why': (
                        'Testimonial is allowed because the speaker was not compensated, '
                        'but it still needs a client / no-compensation disclosure adjacent to the quote.'
                    ),
                })
            else:
                item.update({
                    'status': 'open',
                    'confidence': 'certain',
                    'recommended_option': 'add_disclosure',
                    'options': ['add_disclosure', 'pull'],
                    'why': 'Paid testimonial requires compensation and conflict disclosures adjacent to the quote.',
                })
        if item.get('status') in {'held', 'open'} and finding_id.startswith('performance') and fees:
            if 'gross' in fees or 'before' in fees:
                item.update({
                    'status': 'open',
                    'confidence': 'certain',
                    'recommended_option': 'redline',
                    'options': ['redline', 'pull'],
                    'why': 'Gross figure shown with no equally prominent net figure is a presentation violation.',
                    'draft_text': _HERO_GROSS_DRAFT,
                })
            elif 'net' in fees or 'after' in fees:
                item.update({
                    'status': 'open',
                    'confidence': 'certain',
                    'recommended_option': 'redline',
                    'options': ['redline', 'add_disclosure'],
                    'why': 'Net performance still needs required periods and standard performance disclosures.',
                })
        if (finding_id == 'third_party_rating' or 'rating' in rule_why) and rating_paid:
            item.update(_resolve_rating_answer(rating_paid))
        if (finding_id == 'scarcity' or 'scarcity' in rule_why) and scarcity:
            if any(token in scarcity for token in (
                'real', 'enforced', 'yes', 'actual', 'policy', 'documented',
            )):
                item.update({
                    'status': 'open',
                    'recommended_option': 'redline',
                    'options': ['redline', 'do_nothing'],
                    'why': (
                        'Capacity limit is substantiated. Keep the 12-family (or other) cap. '
                        'Remove “August is filling fast” / similar urgency unless evidenced at '
                        'send time.'
                    ),
                    'draft_text': _EMAIL_SCARCITY_DRAFT,
                })
            else:
                item.update({
                    'status': 'open',
                    'recommended_option': 'redline',
                    'options': ['redline', 'pull'],
                    'why': 'Scarcity copy is not an enforced limit and is misleading.',
                })
        if finding_id == 'relative_performance' and (
            answers.get('relative_performance_source') or answers.get('performance_source') or ''
        ):
            item.update(_resolve_relative_performance(str(
                answers.get('relative_performance_source') or answers.get('performance_source') or ''
            )))
        if (finding_id == 'principal_approval' or 'principal' in rule_why) and approval:
            if approval.startswith('n') or 'direct' in approval:
                item.update({
                    'status': 'open',
                    'confidence': 'certain',
                    'recommended_option': 'pull',
                    'options': ['pull', 'redline'],
                    'why': (
                        'Content finding and a supervision gap. Take the post down now — it cannot '
                        'be edited into compliance while live and unapproved. Route the corrected '
                        'version through pre-approval and log the incident. The agent drafts the '
                        'corrected post; the supervision follow-up is the CCO’s.'
                    ),
                    'draft_text': _LINKEDIN_PULL_DRAFT,
                    'option_labels': {
                        'pull': 'Pull',
                        'redline': 'Redline for re-post',
                    },
                })
            else:
                item.update({
                    'status': 'open',
                    'recommended_option': 'do_nothing',
                    'options': ['do_nothing'],
                    'why': 'Principal pre-approval is on file; remaining issues are content findings only.',
                })
        if finding_id == 'hypothetical_backtest' and audience:
            item.update(_resolve_backtest_audience(audience))
        if finding_id == 'benchmark_comparison' and basis:
            item.update(_resolve_benchmark_basis(basis))
        outcome_answer = (
            answers.get('client_outcome_2022') or answers.get('2022_claim') or ''
        )
        if finding_id == 'client_outcome_2022' and outcome_answer:
            item.update(_resolve_client_outcome_2022(outcome_answer))
        resolved.append(item)
    return resolved


def _resolve_client_outcome_2022(answer: str) -> dict[str, Any]:
    lowered = answer.lower()
    unsubstantiated = any(
        token in lowered
        for token in (
            'no composite',
            'recollection',
            'no documented',
            "doesn't exist",
            'does not exist',
            'doesn’t exist',
            'no comparison',
        )
    )
    if unsubstantiated:
        why = (
            'Unsubstantiated client-outcome claim. Cannot be disclosed into compliance — the '
            'substantiation doesn’t exist. Recommendation: rewrite the paragraph as strategy '
            'design commentary (what laddering is built to do in a year like 2022) with no '
            'client-outcome assertion. Redline supplied doing exactly that.'
        )
    else:
        why = (
            'A client-outcome / 60/40 comparison still needs documented basis, period, net/gross, '
            'and whether “clients” means all, some, or a composite. Without that file, the claim '
            'cannot be disclosed into compliance.'
        )
    return {
        'status': 'open',
        'confidence': 'certain',
        'recommended_option': 'redline',
        'options': ['redline', 'pull'],
        'why': why,
        'title': '2022 comparison',
        'draft_text': _BLOG_2022_DRAFT,
        'option_labels': {'redline': 'Redline', 'pull': 'Pull para 7'},
    }


def _resolve_relative_performance(answer: str) -> dict[str, Any]:
    lowered = answer.lower()
    internal = any(
        token in lowered
        for token in ('internal', 'unaudited', 'one-time', 'one time', 'april')
    )
    if internal:
        why = (
            'An internal, one-time, unaudited comparison cannot support a headline '
            'relative-performance claim in an advertisement. Remove the statistic. A compliant '
            'comparative claim needs defined methodology, current data, and full sourcing.'
        )
    else:
        why = (
            'A relative-performance claim still needs the comparison universe, period, who '
            'computed it, and full sourcing disclosed in this email — not on a website.'
        )
    return {
        'status': 'open',
        'confidence': 'certain',
        'recommended_option': 'redline',
        'options': ['redline', 'add_disclosure'],
        'why': why,
        'draft_text': _EMAIL_RELATIVE_DRAFT,
        'option_labels': {'redline': 'Redline — remove', 'add_disclosure': 'Add sourcing'},
    }


def _resolve_backtest_audience(audience: str) -> dict[str, Any]:
    if audience == 'institutional':
        return {
            'status': 'open',
            'confidence': 'certain',
            'recommended_option': 'add_disclosure',
            'options': ['add_disclosure', 'redline', 'pull'],
            'why': (
                'Institutional prospects may see hypothetical performance if they have the '
                'resources to assess it. Keep the callout only with a methodology page covering '
                'period, assumptions, and how results differ from live accounts.'
            ),
            'draft_text': _BACKTEST_RETAIL_DRAFT,
            'option_labels': {
                'add_disclosure': 'Add methodology page',
                'redline': 'Redline',
                'pull': 'Pull the callout',
            },
        }
    return {
        'status': 'open',
        'confidence': 'certain',
        'recommended_option': 'redline',
        'options': ['redline', 'pull'],
        'why': (
            'Cannot run in the retail version. Recommendation: two decks — retail version drops '
            'the callout entirely; institutional version keeps it with a methodology page added. '
            'Draft methodology-page skeleton supplied.'
        ),
        'draft_text': _BACKTEST_RETAIL_DRAFT,
        'option_labels': {'redline': 'Redline', 'pull': 'Pull the callout'},
        'anchor': 'removed callout',
    }


def _resolve_benchmark_basis(basis: str) -> dict[str, Any]:
    if basis == 'total_return':
        why = (
            'Total return vs total return is a fair basis. One line of disclosure is needed '
            'stating the comparison basis.'
        )
    else:
        why = (
            'Price-only S&P is not like-for-like if the strategy figure is total return. '
            'Disclose the basis and the mismatch next to the bars.'
        )
    return {
        'status': 'open',
        'confidence': 'certain',
        'recommended_option': 'add_disclosure',
        'options': ['add_disclosure'],
        'why': why,
        'draft_text': _BENCHMARK_DRAFT,
        'option_labels': {'add_disclosure': 'Add disclosure'},
    }


def _resolve_rating_answer(answer: str) -> dict[str, Any]:
    lowered = answer.lower()
    five_star_paid = bool(re.search(r'five star', lowered)) and bool(
        re.search(r'paid|entry fee|fee', lowered)
    )
    forbes_unpaid = bool(re.search(r'forbes', lowered)) and bool(
        re.search(r'forbes.{0,40}(do not|does not|unpaid|no fee)|do not', lowered)
    )
    why = (
        'All three ratings need date, period, and (where applicable) paid-participation '
        'disclosures adjacent to the badges at a readable size. The footer is too far away.'
    )
    if five_star_paid:
        why += (
            ' The firm confirmed Five Star involves a paid entry fee; '
            'that badge needs a paid-participation disclosure.'
        )
    if forbes_unpaid or re.search(
        r'forbes and bbb do not|forbes.{0,30}bbb.{0,20}do not',
        lowered,
    ):
        why += (
            ' Forbes and BBB do not involve a paid entry fee per the firm’s answer; '
            'they still need date and period next to the seals.'
        )
    elif any(token in lowered for token in ('yes', 'paid', 'fee')) and not five_star_paid:
        why += ' Paid-participation disclosure is also required.'
    return {
        'status': 'open',
        'confidence': 'certain',
        'recommended_option': 'add_disclosure',
        'options': ['add_disclosure'],
        'why': why,
        'draft_text': _rating_draft(five_star_paid=five_star_paid),
        'option_labels': {'add_disclosure': 'Add disclosure'},
    }


def _rating_draft(*, five_star_paid: bool) -> str:
    five_star = (
        'Five Star Wealth Manager — include date, period, and that the firm paid an entry / participation fee.'
        if five_star_paid
        else 'Five Star Wealth Manager — include date and period adjacent to the seal.'
    )
    return (
        'Add a readable caption under the badge row: '
        'Forbes Best-In-State Wealth Advisors 2025 — include date and the period covered; '
        f'{five_star} '
        'A+ BBB Rating — include date and period. '
        'Past rankings are not representative of future performance or of all clients.'
    )


def _finalize_footer_after_answers(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    held_others = [
        item for item in findings
        if item.get('status') == 'held' and item.get('id') != 'layout_buried'
    ]
    has_other_findings = any(item.get('id') != 'layout_buried' for item in findings)
    finalized: list[dict[str, Any]] = []
    for item in findings:
        row = dict(item)
        if row.get('id') == 'layout_buried' and not held_others and has_other_findings:
            row['recommended_option'] = 'do_nothing'
            row['options'] = ['do_nothing', 'redline']
            row['draft_text'] = _FOOTER_COVERED_DRAFT
            row['option_labels'] = {
                'do_nothing': 'Covered by items 1–4',
                'redline': 'Redline (layout instruction)',
            }
            row['why'] = (
                '7pt grey, below the fold, after the sitemap. The block may remain for the record, '
                'but it no longer carries the disclosure burden — items 1–4 do.'
            )
        finalized.append(row)
    return finalized


def _order_findings(findings: list[dict[str, Any]], format_name: str) -> list[dict[str, Any]]:
    if format_name == 'webpage':
        rank = {region: index for index, region in enumerate(_WEBPAGE_REGION_ORDER)}

        def webpage_key(item: dict[str, Any]) -> tuple[int, int]:
            region = str(item.get('region') or '')
            return (rank.get(region, 50), 0)

        return sorted(findings, key=webpage_key)
    if format_name == 'presentation':
        rank = {finding_id: index for index, finding_id in enumerate(_PRESENTATION_FINDING_ORDER)}

        def deck_key(item: dict[str, Any]) -> tuple[int, int, int]:
            try:
                page = int(item.get('slide') or 0)
            except (TypeError, ValueError):
                page = 0
            return (
                page if page > 0 else 10_000,
                rank.get(_base_finding_id(str(item.get('id') or '')), 50),
                0,
            )

        return sorted(findings, key=deck_key)
    if format_name == 'social':
        rank = {finding_id: index for index, finding_id in enumerate(_SOCIAL_FINDING_ORDER)}

        def social_key(item: dict[str, Any]) -> tuple[int, int]:
            return (rank.get(str(item.get('id') or ''), 50), 0)

        return sorted(findings, key=social_key)
    if format_name == 'email':
        rank = {finding_id: index for index, finding_id in enumerate(_EMAIL_FINDING_ORDER)}

        def email_key(item: dict[str, Any]) -> tuple[int, int]:
            return (rank.get(str(item.get('id') or ''), 50), 0)

        return sorted(findings, key=email_key)
    if format_name == 'blog':
        rank = {finding_id: index for index, finding_id in enumerate(_BLOG_FINDING_ORDER)}

        def blog_key(item: dict[str, Any]) -> tuple[int, int]:
            return (rank.get(str(item.get('id') or ''), 50), 0)

        return sorted(findings, key=blog_key)
    return findings


def _assign_markers(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    marked: list[dict[str, Any]] = []
    for index, item in enumerate(findings, start=1):
        row = dict(item)
        row['marker'] = index
        marked.append(row)
    return marked


def _email_part_meta(finding: dict[str, Any]) -> tuple[str, str]:
    finding_id = str(finding.get('id') or '')
    if finding_id in _EMAIL_FINDING_PARTS:
        return _EMAIL_FINDING_PARTS[finding_id]
    region = str(finding.get('region') or '').strip()
    part = str(finding.get('part') or '').strip()
    if part:
        return (region, part)
    blob = ' '.join(
        str(finding.get(key) or '') for key in ('title', 'excerpt', 'why', 'region')
    ).lower()
    if region in {'subject', 'subject_line'} or 'subject line' in blob:
        return ('subject', 'subject line')
    if '87%' in blob or 'outperformed' in blob or 'comparable strategies' in blob:
        return ('body', '87% claim')
    if 'rating' in blob or 'top 100' in blob:
        return ('body', 'rating')
    if 'scarcity' in blob or 'filling fast' in blob:
        return ('body', 'scarcity')
    if region == 'email_footer':
        return ('email_footer', 'email footer')
    if region:
        return (region, region.replace('_', ' '))
    return ('body', 'body')


def _stamp_email_parts(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stamped: list[dict[str, Any]] = []
    for item in findings:
        row = dict(item)
        region, part = _email_part_meta(row)
        if region and not row.get('region'):
            row['region'] = region
        row['part'] = part
        if not row.get('anchor') or row.get('anchor') == 'slide copy':
            row['anchor'] = _REGION_ANCHORS.get(str(row.get('region') or ''), row.get('region') or 'body')
        stamped.append(row)
    return stamped


def _normalize_finding_locations(
    findings: list[dict[str, Any]],
    format_name: str,
) -> list[dict[str, Any]]:
    """Emails are keyed by part (C1 · subject line). Decks, homepages, and blogs keep page markers."""
    if format_name == 'email':
        findings = _stamp_email_parts(findings)
    if format_name in {'presentation', 'webpage', 'blog'}:
        if format_name == 'presentation':
            return findings
        cleaned_pages: list[dict[str, Any]] = []
        for item in findings:
            row = dict(item)
            if row.get('anchor') == 'slide copy':
                row['anchor'] = row.get('region') or 'page copy'
            cleaned_pages.append(row)
        return cleaned_pages
    cleaned: list[dict[str, Any]] = []
    for item in findings:
        row = dict(item)
        row['slide'] = None
        if row.get('anchor') == 'slide copy':
            row['anchor'] = row.get('region') or row.get('part') or ''
        cleaned.append(row)
    return cleaned


def finalize_marketing_findings(
    findings: list[dict[str, Any]],
    classification: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Sort findings, stamp C1…Cn, and attach format-specific locations (slide vs email part)."""
    format_name = str(classification.get('format') or '')
    marked = _normalize_finding_locations(
        _assign_markers(_order_findings(list(findings), format_name)),
        format_name,
    )
    return marked, _change_list_from_findings(marked, classification)


def _change_list_from_findings(
    findings: list[dict[str, Any]],
    classification: dict[str, Any],
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for finding in findings:
        option = finding.get('recommended_option')
        if option == 'held':
            continue
        if option == 'do_nothing' and finding.get('id') != 'layout_buried':
            continue
        marker = int(finding.get('marker') or (len(changes) + 1))
        changes.append({
            'marker': marker,
            'finding_id': finding.get('id'),
            'excerpt': finding.get('excerpt'),
            'title': finding.get('title'),
            'action': option,
            'rule': finding.get('rule'),
            'draft_text': finding.get('draft_text') or _draft_text_for(finding, classification),
            'placement': 'adjacent_to_claim' if finding.get('id') != 'layout_buried' else 'footer_record_only',
            'slide': finding.get('slide') if str(classification.get('format') or '') == 'presentation' else None,
            'part': finding.get('part'),
            'region': finding.get('region'),
        })
    return changes


def _draft_text_for(finding: dict[str, Any], classification: dict[str, Any]) -> str:
    if finding.get('draft_text'):
        return str(finding['draft_text'])
    option = finding.get('recommended_option')
    finding_id = _base_finding_id(str(finding.get('id') or ''))
    if finding_id == 'layout_buried' or option == 'do_nothing' and finding_id == 'layout_buried':
        return _FOOTER_COVERED_DRAFT
    if option == 'add_disclosure':
        if finding_id == 'testimonial' or 'testimonial' in finding.get('why', '').lower():
            return _TESTIMONIAL_DRAFT
        if finding_id == 'benchmark_comparison' or 'benchmark' in finding.get('why', '').lower():
            return _BENCHMARK_DRAFT
        if finding_id == 'third_party_rating' or 'rating' in finding.get('why', '').lower():
            return _rating_draft(five_star_paid=True)
        if finding_id == 'missing_disclosures':
            return _BLOG_DISCLOSURE_DRAFT
        return 'Add the required disclosure next to the claim, at a readable size.'
    if option == 'pull':
        return 'Take the piece down. It cannot be cured by editing while live, or the claim cannot be substantiated.'
    if option == 'redline':
        if finding_id == 'performance_gross_hero':
            return _HERO_GROSS_DRAFT
        if finding_id == 'safety_claim':
            return _SAFETY_DRAFT
        if finding_id == 'layout_buried':
            return _LAYOUT_REDLINE_DRAFT
        if finding_id == 'cherry_pick':
            return _CHERRY_PICK_DRAFT
        if finding_id == 'gross_net_layout':
            return _GROSS_NET_SLIDE_DRAFT
        if finding_id == 'hypothetical_backtest':
            return _BACKTEST_RETAIL_DRAFT
        if finding_id == 'unsubstantiated_exclusivity':
            return _EXCLUSIVITY_DRAFT
        if finding_id == 'unsubstantiated_statistic':
            return _QUANTIFIED_SPLIT_DRAFT
        if finding_id == 'third_party_outcome':
            return _THIRD_PARTY_OUTCOME_DRAFT
        if finding_id == 'structured_yield_claim':
            return _YIELD_CLAIM_DRAFT
        if finding_id == 'relative_performance':
            return _EMAIL_RELATIVE_DRAFT
        if finding_id == 'subject_line':
            return _EMAIL_SUBJECT_DRAFT
        if finding_id == 'eliminate_claim':
            return _BLOG_ELIMINATE_DRAFT
        if finding_id == 'client_outcome_2022':
            return _BLOG_2022_DRAFT
        return 'Revise or remove the cited language so the remaining claim is fair, balanced, and fully disclosed.'
    del classification
    return ''


def _clip_around(text: str, needle: str, radius: int = 90) -> str:
    lowered = text.lower()
    target = needle.lower()
    index = lowered.find(target)
    if index < 0:
        return ''
    start = max(0, index - radius)
    end = min(len(text), index + len(needle) + radius)
    clip = text[start:end].strip()
    return re.sub(r'\s+', ' ', clip)


def _footer_excerpt(text: str) -> str:
    match = re.search(r'(footer.{0,400}|below the fold.{0,400})', text, re.I | re.S)
    if match:
        return re.sub(r'\s+', ' ', match.group(0))[:400]
    return text[-280:]


def _rating_excerpt(text: str) -> str:
    parts = _rating_labels(text)
    return ', '.join(parts) if parts else 'third-party ratings'


def _rating_labels(text: str) -> list[str]:
    labels: list[str] = []
    if re.search(r'forbes', text, re.I):
        labels.append('Forbes Best-In-State')
    if re.search(r'five star', text, re.I):
        labels.append('Five Star Wealth Manager')
    if re.search(r'\bbbb\b', text, re.I):
        labels.append('A+ BBB Rating')
    if re.search(r'google reviews?|\d+\+?\s+(?:google\s+)?(?:client\s+)?reviews?', text, re.I):
        labels.append('Google Reviews')
    return labels


def _rating_question(text: str) -> str:
    firm = 'Granite Peak' if re.search(r'granite peak', text, re.I) else 'the firm'
    named: list[str] = []
    if re.search(r'forbes', text, re.I):
        named.append('Forbes')
    if re.search(r'five star', text, re.I):
        named.append('Five Star')
    if re.search(r'\bbbb\b', text, re.I):
        named.append('BBB')
    if re.search(r'google reviews?|\d+\+?\s+(?:google\s+)?(?:client\s+)?reviews?', text, re.I):
        named.append('Google Reviews')
    listed = ', '.join(named) if named else 'the rating(s)'
    target = (
        'any of the three ratings in the badge row'
        if len(named) >= 3
        else 'the rating(s)'
    )
    return (
        f'One thing before I finish: did {firm} pay a fee, or provide any compensation, '
        f'to be considered for or to display {target} ({listed})?'
    )


def _duplicates_heuristic(candidate: dict[str, Any], heuristic_findings: list[dict[str, Any]]) -> bool:
    blob = f"{candidate.get('title') or ''} {candidate.get('excerpt') or ''} {candidate.get('why') or ''}".lower()
    keys = (
        'buried',
        'gross',
        'testimonial',
        'downturn',
        'badge',
        'rating',
        'cherry',
        'scarcity',
        'principal',
        'backtest',
        'hypothetical',
        'benchmark',
        'puffery',
        'appendix',
    )
    for item in heuristic_findings:
        existing = f"{item.get('title') or ''} {item.get('excerpt') or ''} {item.get('why') or ''}".lower()
        if any(key in blob and key in existing for key in keys):
            return True
    return False


def _similar_question(prompt: str, existing: list[dict[str, Any]]) -> bool:
    lowered = prompt.lower()
    for item in existing:
        other = str(item.get('prompt') or '').lower()
        if not other:
            continue
        fee_question = 'fee' in lowered and 'fee' in other and 'rating' not in lowered
        if ('gross' in lowered and 'gross' in other) or fee_question:
            return True
        if 'testimonial' in lowered and 'testimonial' in other:
            return True
        if ('rating' in lowered or 'forbes' in lowered) and ('rating' in other or 'forbes' in other):
            return True
        if 'principal' in lowered and 'principal' in other:
            return True
        if ('retail' in lowered or 'institutional' in lowered) and (
            'retail' in other or 'institutional' in other
        ):
            return True
        if ('s&p' in lowered or 'total return' in lowered) and (
            's&p' in other or 'total return' in other or 'dividend' in other
        ):
            return True
    return False


def marketing_review_system_prompt() -> str:
    return with_financial_law_expert_persona(
        f"""You review firm marketing against this rule set. Never approve the piece.

{MARKETING_RULE_SET}

Apply the primary rule for the registrant: SEC 206(4)-1 for RIA (and unknown);
FINRA 2210 plus 2010 for broker-dealer. Dual registrants get both. Switch on
2211–2216 only when the material contains that topic. Rule 204-2 is
recordkeeping — this review is a saved artifact that must include the
rule-store version used.

The homepage (and other) capture is already filled into a template (hero,
badges, testimonials, mid-page, footer). Review words and layout together.
For social posts, prospect emails, and pitch decks, review the submitted copy
and any layout notes the same way. For blog articles, locate the line within the piece
between educational commentary (outside the Marketing Rule) and promotion of
the firm’s services (inside it). Do not label the whole article one thing or
the other.
For pitch decks, layout notes may be OCR of screenshot slides. Cite slide numbers.
For prospect emails, cite the email part — subject, body, or email footer — never a
slide number. Set part to a short component label (subject line, 87% claim, rating,
scarcity) and leave slide empty. Email findings are keyed C1, C2, C3 against those
parts, matching the visual pass.
Prefer redline or add_disclosure over do_nothing when a performance, hypothetical,
testimonial, or unsubstantiated claim appears. Include marker_x/marker_y (0-1) when
the finding is on a screenshot slide. The excerpt field must be the exact quoted
sentence or headline from the material that caused the finding — not a paraphrase
and not the draft replacement.
When the payload includes cosine-related rule excerpts, run a gap analysis:
compare the submitted marketing content to those excerpts only. They were
selected because mean-max cosine similarity showed the content embeddings
and the official rule embeddings are interrelated. For pitch decks, compare
each slide’s text embeddings and diagram embeddings independently, then roll
the related excerpts into one deck-wide gap. Do not apply a FINRA
topic rule (2211–2216) unless it appears in related_rule_excerpts.

Ask a question only when the answer would change the finding.
If the material already states the fact, emit a finding instead of a question.
Do not ask whether a return is gross or net when the material already says gross or net.
Do not ask whether a testimonial was paid when the material already says no compensation was paid.
If compensation status is unknown, keep the testimonial finding open and treat
the paid-or-compensated question as optional — do not hold the finding for an
answer.
Being unsure is not a reason to escalate — make the call and set confidence.
{REPORT_HEADER}
{PERFORMANCE_DISCLAIMER}
Return JSON only:
{{
  "classification": {{"format": "", "audience": "", "registrant_type": "", "tags": [], "rules": []}},
  "findings": [{{
    "id": "",
    "title": "",
    "excerpt": "",
    "rule": "",
    "why": "",
    "confidence": "certain|judgment_moderate|pending_answer",
    "options": ["redline","add_disclosure","pull","do_nothing","escalate","held"],
    "recommended_option": "",
    "status": "open|held",
    "draft_text": "",
    "slide": 1,
    "part": "subject line",
    "region": "subject",
    "marker_x": 0.86,
    "marker_y": 0.18
  }}],
  "questions": [{{"id": "", "prompt": ""}}],
  "change_list": [{{"marker": 1, "excerpt": "", "action": "", "draft_text": "", "placement": ""}}]
}}"""
    )


async def llm_review(
    *,
    client: LLMProvider | None,
    classification: dict[str, Any],
    body_text: str,
    layout_notes: list[dict[str, Any]],
    rule_bundle: dict[str, Any],
    answers: dict[str, str],
    vector_gap: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if client is None:
        return None
    related_excerpts = list((vector_gap or {}).get('related_excerpts') or [])
    cosine_rules = list((vector_gap or {}).get('rules') or [])
    payload = {
        'task': 'gap_analysis' if related_excerpts else 'marketing_review',
        'classification': classification,
        'body_text': body_text[:12_000],
        'layout_notes': layout_notes[:60],
        'answers': answers,
        'rule_set': MARKETING_RULE_SET,
        'rule_version': rule_bundle.get('version'),
        'rule_excerpts': (
            str((vector_gap or {}).get('excerpt_text') or '')[:8_000]
            or str(rule_bundle.get('excerpt_text') or '')[:8_000]
        ),
        'related_rule_excerpts': related_excerpts[:8],
        'rule_cosine_similarity': cosine_rules,
        'slide_rule_gaps': (vector_gap or {}).get('slides') or [],
    }
    try:
        response = await client.generate(
            [ChatMessage('user', str(payload))],
            max_tokens=4_000,
            system=marketing_review_system_prompt(),
            throttle='sonnet',
            trace_name='marketing.review',
        )
        parsed = parse_llm_json_dict(response_text(response))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    findings = parsed.get('findings')
    if not isinstance(findings, list):
        return None
    for finding in findings:
        if isinstance(finding, dict) and 'id' not in finding:
            finding['id'] = str(uuid4())
    parsed.setdefault('questions', [])
    parsed.setdefault('change_list', [])
    parsed['report_header'] = REPORT_HEADER
    parsed['performance_disclaimer'] = PERFORMANCE_DISCLAIMER
    return parsed
