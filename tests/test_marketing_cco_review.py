from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.operators import eq, ne

from app.models.core import GapAnalysis, MarketingContent, Policy, PolicyRevision
from app.services.marketing_gap_bridge import (
    apply_selected_options,
    ensure_marketing_shell_policy,
    impact_for_marketing_finding,
    map_marketing_findings,
    marketing_shell_marker,
    marketing_shell_policy_name,
    persist_marketing_gap_analysis,
)
from app.services.marketing_redline_service import (
    build_marketing_redline_bytes,
    format_layout_notes_template,
    redline_bytes_have_rule_gap_fix,
    send_marketing_review_to_cco_queue,
    tracked_replacements_from_findings,
)
from app.services.policy_types import MARKETING_CONTENT_TYPE
from app.services.tracked_redline_document import docx_has_track_changes


def _social_finding(**overrides) -> dict:
    finding = {
        'id': 'perf_claim',
        'excerpt': 'Beat the market by 12% last year',
        'title': 'performance claim',
        'rule': '206(4)-1',
        'why': 'Gross performance without net presentation or time period required by the Marketing Rule.',
        'confidence': 'certain',
        'recommended_option': 'redline',
        'options': ['redline', 'pull', 'do_nothing'],
        'option_labels': {'redline': 'Redline', 'pull': 'Pull'},
        'status': 'open',
        'draft_text': 'Past performance is not a guarantee of future results. Net of fees, returns were 8%.',
    }
    finding.update(overrides)
    return finding


def test_selected_options_drive_gap_impact_and_display() -> None:
    findings = apply_selected_options(
        [_social_finding()],
        [{'finding_id': 'perf_claim', 'option': 'pull'}],
    )
    mapped = map_marketing_findings(findings, format_name='social')
    assert mapped[0]['sentence_label'] == 'Social post excerpt'
    assert mapped[0]['selected_option'] == 'pull'
    assert mapped[0]['recommended_option'] == 'redline'
    assert mapped[0]['impact_score'] == 'HIGH'
    assert 'selected' in mapped[0]['options_display'].lower()
    assert impact_for_marketing_finding(findings[0]) == 'HIGH'


def test_persist_marketing_gap_analysis_for_social_selected_options(monkeypatch) -> None:
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')
    monkeypatch.setattr(
        'app.services.marketing_gap_bridge.ensure_marketing_shell_policy',
        lambda db, content: policy,
    )
    stored: list[object] = []

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return None

        def all(self):
            return []

        def delete(self, synchronize_session=False):
            return 0

    class _Db:
        def query(self, model):
            return _Query()

        def add(self, obj):
            stored.append(obj)

        def flush(self):
            return None

        def delete(self, obj):
            return None

    content = SimpleNamespace(
        id='c-social',
        org_id='org-a',
        format='social',
        title='Q3 ad',
        name='Q3 ad',
        source_url=None,
        classification={'rules': ['206(4)-1', '2210']},
    )
    review = SimpleNamespace(
        id='rev-social',
        findings=[_social_finding()],
        selected_options=[{'finding_id': 'perf_claim', 'option': 'redline'}],
        classification={'rules': ['206(4)-1', '2210']},
    )
    gap_id = persist_marketing_gap_analysis(
        _Db(),
        content=content,
        review=review,
        findings=list(review.findings),
        selected_options=[{'finding_id': 'perf_claim', 'option': 'redline'}],
        for_cco_review=True,
    )
    assert gap_id
    gap = stored[0]
    assert gap.trigger_type == 'marketing_content'
    assert gap.impact_score == 'HIGH'
    assert gap.retrieval_metadata['redline_eligible'] is True
    assert gap.retrieval_metadata['marketing_format'] == 'social'
    assert 'SEC: Rule 206(4)-1' in gap.retrieval_metadata['marketing_rule_labels']
    assert 'sent to CCO review' in gap.gap_summary


def test_persist_for_cco_review_creates_gap_without_mapped_findings(monkeypatch) -> None:
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')
    monkeypatch.setattr(
        'app.services.marketing_gap_bridge.ensure_marketing_shell_policy',
        lambda db, content: policy,
    )
    stored: list[object] = []

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return None

        def all(self):
            return []

        def delete(self, synchronize_session=False):
            return 0

    class _Db:
        def query(self, model):
            return _Query()

        def add(self, obj):
            stored.append(obj)

        def flush(self):
            return None

        def delete(self, obj):
            return None

    content = SimpleNamespace(
        id='c-empty',
        org_id='org-a',
        format='social',
        title='Q3 ad',
        name='Q3 ad',
        source_url=None,
        classification={'rules': ['206(4)-1']},
    )
    review = SimpleNamespace(
        id='rev-empty',
        findings=[],
        selected_options=[],
        classification={'rules': ['206(4)-1']},
    )
    gap_id = persist_marketing_gap_analysis(
        _Db(),
        content=content,
        review=review,
        findings=[],
        selected_options=[],
        for_cco_review=True,
    )
    assert gap_id
    gap = stored[0]
    assert gap.trigger_type == 'marketing_content'
    assert gap.impact_score == 'MEDIUM'
    assert gap.retrieval_metadata['redline_eligible'] is True
    assert 'sent to CCO review' in gap.gap_summary


def test_send_marketing_review_to_cco_queue_creates_pending_revision(monkeypatch) -> None:
    gap = SimpleNamespace(
        id='gap-1',
        org_id='org-a',
        policy_id='policy-1',
        impact_score='HIGH',
        publication_id=None,
        gap_summary='Social post review sent to CCO review',
    )
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')
    stored: list[object] = []

    class _Query:
        def __init__(self, row=None):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return None

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            if model is GapAnalysis:
                return _Query(gap)
            if model is Policy:
                return _Query(policy)
            return _Query(None)

        def add(self, obj):
            stored.append(obj)

        def flush(self):
            return None

    monkeypatch.setattr(
        'app.services.marketing_redline_service.persist_marketing_gap_analysis',
        lambda *args, **kwargs: 'gap-1',
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.build_redline_for_marketing_gap',
        lambda *args, **kwargs: b'fake-docx',
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.attach_marketing_redline_file',
        lambda db, revision, policy, redline_bytes: revision,
    )

    revision, notify = send_marketing_review_to_cco_queue(
        _Db(),
        content=SimpleNamespace(id='c-1', org_id='org-a'),
        review=SimpleNamespace(id='r-1', findings=[], selected_options=[]),
        selected_options=[{'finding_id': 'f1', 'option': 'redline'}],
    )
    assert isinstance(revision, PolicyRevision)
    assert revision.status == 'PENDING'
    assert revision.review_requested_at is not None
    assert revision.gap_analysis_id == 'gap-1'
    assert notify is not None
    assert notify['revision_id'] == revision.id
    assert stored and stored[0] is revision


def test_send_marketing_review_preview_does_not_enqueue(monkeypatch) -> None:
    gap = SimpleNamespace(
        id='gap-1',
        org_id='org-a',
        policy_id='policy-1',
        impact_score='HIGH',
        publication_id=None,
        gap_summary='Social post review',
    )
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')
    stored: list[object] = []

    class _Query:
        def __init__(self, row=None):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return None

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            if model is GapAnalysis:
                return _Query(gap)
            if model is Policy:
                return _Query(policy)
            return _Query(None)

        def add(self, obj):
            stored.append(obj)

        def flush(self):
            return None

    monkeypatch.setattr(
        'app.services.marketing_redline_service.persist_marketing_gap_analysis',
        lambda *args, **kwargs: 'gap-1',
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.build_redline_for_marketing_gap',
        lambda *args, **kwargs: b'fake-docx',
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.attach_marketing_redline_file',
        lambda db, revision, policy, redline_bytes: revision,
    )

    revision, notify = send_marketing_review_to_cco_queue(
        _Db(),
        content=SimpleNamespace(id='c-1', org_id='org-a'),
        review=SimpleNamespace(id='r-1', findings=[], selected_options=[]),
        selected_options=[{'finding_id': 'f1', 'option': 'redline'}],
        enqueue=False,
    )
    assert isinstance(revision, PolicyRevision)
    assert revision.status == 'PENDING'
    assert revision.review_requested_at is None
    assert notify is None
    assert stored and stored[0] is revision


def test_send_marketing_review_enqueues_existing_preview(monkeypatch) -> None:
    gap = SimpleNamespace(
        id='gap-1',
        org_id='org-a',
        policy_id='policy-1',
        impact_score='HIGH',
        publication_id=None,
        gap_summary='Social post review sent to CCO review',
        retrieval_metadata={'homepage_rule_similarity': None},
    )
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')
    existing = PolicyRevision(
        id='rev-preview',
        gap_analysis_id='gap-1',
        policy_id='policy-1',
        org_id='org-a',
        redline_file_url='s3://bucket/redline.docx',
        status='PENDING',
        review_requested_at=None,
    )

    class _Query:
        def __init__(self, row=None):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return self.row

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            if model is GapAnalysis:
                return _Query(gap)
            if model is Policy:
                return _Query(policy)
            if model is PolicyRevision:
                return _Query(existing)
            return _Query(None)

        def flush(self):
            return None

    monkeypatch.setattr(
        'app.services.marketing_redline_service.persist_marketing_gap_analysis',
        lambda *args, **kwargs: 'gap-1',
    )

    revision, notify = send_marketing_review_to_cco_queue(
        _Db(),
        content=SimpleNamespace(id='c-1', org_id='org-a'),
        review=SimpleNamespace(id='r-1', findings=[], selected_options=[]),
        selected_options=[{'finding_id': 'f1', 'option': 'redline'}],
        enqueue=True,
    )
    assert revision is existing
    assert revision.review_requested_at is not None
    assert notify is not None
    assert notify['revision_id'] == 'rev-preview'


def test_send_marketing_review_keeps_queue_row_if_redline_fails(monkeypatch) -> None:
    gap = SimpleNamespace(
        id='gap-1',
        org_id='org-a',
        policy_id='policy-1',
        impact_score='HIGH',
        publication_id=None,
        gap_summary='Social post review sent to CCO review',
    )
    policy = SimpleNamespace(id='policy-1', org_id='org-a', name='Social post — Q3 ad')

    class _Query:
        def __init__(self, row=None):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args):
            return self

        def first(self):
            return None

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            if model is GapAnalysis:
                return _Query(gap)
            if model is Policy:
                return _Query(policy)
            return _Query(None)

        def add(self, obj):
            return None

        def flush(self):
            return None

    monkeypatch.setattr(
        'app.services.marketing_redline_service.persist_marketing_gap_analysis',
        lambda *args, **kwargs: 'gap-1',
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.build_redline_for_marketing_gap',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('docx failed')),
    )

    revision, notify = send_marketing_review_to_cco_queue(
        _Db(),
        content=SimpleNamespace(id='c-1', org_id='org-a'),
        review=SimpleNamespace(id='r-1', findings=[], selected_options=[]),
        selected_options=[],
    )
    assert revision is not None
    assert revision.status == 'PENDING'
    assert revision.review_requested_at is not None
    assert revision.redline_file_url is None
    assert notify is not None


def test_marketing_redline_docx_uses_rule_gap_fix_comments() -> None:
    mapped = map_marketing_findings(
        apply_selected_options(
            [_social_finding()],
            [{'finding_id': 'perf_claim', 'option': 'redline'}],
        ),
        format_name='social',
    )
    replacements = tracked_replacements_from_findings(mapped)
    assert replacements
    assert replacements[0].comment
    assert 'RULE:' in replacements[0].comment
    assert 'GAP:' in replacements[0].comment
    assert 'FIX:' in replacements[0].comment

    output = build_marketing_redline_bytes(
        title='Q3 social post',
        format_label='Social post',
        body_text='Beat the market by 12% last year. Book a call today.',
        rule_labels=['SEC: Rule 206(4)-1', 'FINRA: Rule 2210'],
        findings=mapped,
        gap_summary='Social post review',
    )
    assert docx_has_track_changes(output) is True
    assert redline_bytes_have_rule_gap_fix(output) is True
    with ZipFile(BytesIO(output)) as archive:
        comments = archive.read('word/comments.xml').decode()
        document = archive.read('word/document.xml').decode()
    assert 'RULE:' in comments
    assert 'GAP:' in comments
    assert 'FIX:' in comments
    assert '206(4)-1' in comments
    assert 'commentRangeStart' in document
    accepted = document.replace('\n', ' ')
    assert 'Past performance is not a guarantee' in accepted or 'w:ins' in document


GRANITE_PEAK_LAYOUT_NOTES = [
    {
        'region': 'hero',
        'font_note': 'full-width banner, 48pt headline',
        'excerpt': (
            'Retire with confidence. Granite Peak clients have earned an average of '
            '11.4% annually since 2015.'
        ),
    },
    {
        'region': 'badge_row',
        'font_note': 'three large gold seals, directly under hero',
        'excerpt': (
            'Forbes Best-In-State Wealth Advisors 2025 · Five Star Wealth Manager · A+ BBB Rating'
        ),
    },
    {
        'region': 'testimonial',
        'font_note': 'rotating, 24pt type',
        'excerpt': (
            '"Granite Peak doubled my portfolio. I finally sleep at night." — Janet R. · '
            '"The only advisors I trust." — Mark T.'
        ),
    },
    {
        'region': 'mid_page',
        'font_note': '16pt',
        'excerpt': (
            'Our disciplined process has protected client capital in every downturn '
            'since the firm was founded.'
        ),
    },
    {
        'region': 'footer',
        'below_fold': True,
        'font_note': '7pt grey text on white, below the fold, after the sitemap',
        'excerpt': (
            'Past performance is not indicative of future results. Average annual return '
            'figure reflects a composite of discretionary accounts, gross of fees. '
            'Testimonials were given by current clients; no compensation was provided. '
            'Rankings by third parties are not indicative of future investment performance.'
        ),
    },
]


def _granite_peak_findings() -> list[dict]:
    return [
        {
            'id': 'performance_gross_hero',
            'region': 'hero',
            'excerpt': '11.4% annually since 2015',
            'title': 'Hero performance claim — gross only',
            'rule': '206(4)-1',
            'why': 'The net performance figure is missing. Net must match gross in prominence.',
            'recommended_option': 'redline',
            'selected_option': 'redline',
            'options': ['redline', 'pull'],
            'draft_text': (
                '11.4% gross of fees; net of fees X.X% [firm to supply]. '
                'Past performance does not guarantee future results.'
            ),
        },
        {
            'id': 'third_party_rating',
            'region': 'badge_row',
            'excerpt': 'Forbes Best-In-State Wealth Advisors 2025',
            'title': 'Rating badges — no adjacent disclosures',
            'rule': '206(4)-1',
            'why': 'Date, period, and paid/unpaid status are not adjacent to the badges.',
            'recommended_option': 'add_disclosure',
            'selected_option': 'add_disclosure',
            'options': ['add_disclosure'],
            'draft_text': (
                'Add a readable caption under the badge row: '
                'Forbes Best-In-State 2025 (published April 2025, based on 2024 data; no fee paid). '
                'Five Star Wealth Manager 2025 (paid application fee). '
                'Ratings are not indicative of future performance.'
            ),
        },
        {
            'id': 'testimonial',
            'region': 'testimonial',
            'excerpt': 'Granite Peak doubled my portfolio',
            'title': 'Testimonial carousel',
            'rule': '206(4)-1',
            'why': 'Disclosures are buried in the footer. “Doubled my portfolio” is a performance claim.',
            'recommended_option': 'add_disclosure',
            'selected_option': 'add_disclosure',
            'options': ['add_disclosure', 'redline', 'pull'],
            'draft_text': (
                'Statements by current clients; no compensation was provided. '
                'Individual results vary and are not typical. Rotate the “doubled my portfolio” '
                'quote out of the carousel — it is a performance claim.'
            ),
        },
        {
            'id': 'safety_claim',
            'region': 'mid_page',
            'excerpt': 'has protected client capital in every downturn since the firm was founded',
            'title': 'Protected client capital in every downturn',
            'rule': '206(4)-1',
            'why': 'Absolute safety claim that cannot be cured by a disclosure.',
            'recommended_option': 'redline',
            'selected_option': 'redline',
            'options': ['redline', 'pull'],
            'draft_text': (
                'Redline: “has protected client capital in every downturn since the firm was founded” '
                '→ “seeks to manage downside risk”. A disclosure cannot cure an absolute safety claim.'
            ),
        },
        {
            'id': 'layout_buried',
            'region': 'footer',
            'excerpt': 'Past performance is not indicative of future results',
            'title': 'Footer disclosure block — buried',
            'rule': '206(4)-1',
            'why': 'Footer is 7pt grey, below the fold. Covered by items 1–4.',
            'recommended_option': 'do_nothing',
            'selected_option': 'do_nothing',
            'options': ['do_nothing', 'redline'],
            'draft_text': (
                'May remain for the record, but no longer carries the disclosure burden — '
                'items 1–4 must sit adjacent to the claims, at a readable size.'
            ),
        },
    ]


def test_homepage_template_is_visual_pass_layout() -> None:
    template = format_layout_notes_template(GRANITE_PEAK_LAYOUT_NOTES)
    assert '[HERO — full-width banner, 48pt headline]' in template
    assert '[BADGE ROW — three large gold seals, directly under hero]' in template
    assert '[TESTIMONIAL CAROUSEL — rotating, 24pt type]' in template
    assert '[MID-PAGE — 16pt]' in template
    assert '[FOOTER — 7pt grey text on white, below the fold, after the sitemap]' in template
    assert 'Retire with confidence.' in template
    assert 'Forbes Best-In-State Wealth Advisors 2025' in template
    assert 'doubled my portfolio' in template
    assert 'protected client capital in every downturn' in template
    assert 'Past performance is not indicative of future results' in template


def test_homepage_redline_applies_keyed_corrections_onto_template() -> None:
    findings = _granite_peak_findings()
    replacements = tracked_replacements_from_findings(
        findings,
        template_text=format_layout_notes_template(GRANITE_PEAK_LAYOUT_NOTES),
        layout_notes=GRANITE_PEAK_LAYOUT_NOTES,
    )
    assert len(replacements) == 4
    joined_search = ' '.join(item.search_text for item in replacements)
    joined_replace = ' '.join(item.replace_text for item in replacements)
    assert '11.4%' in joined_search
    assert 'net of fees' in joined_replace
    assert 'Forbes Best-In-State' in joined_replace
    assert 'paid application fee' in joined_replace
    assert 'Statements by current clients' in joined_replace
    assert 'doubled my portfolio' not in joined_replace.lower()
    assert 'seeks to manage downside risk' in joined_replace
    assert 'protected client capital in every downturn' not in joined_replace
    assert all('RULE:' in item.comment and 'GAP:' in item.comment and 'FIX:' in item.comment for item in replacements)

    output = build_marketing_redline_bytes(
        title='Granite Peak homepage',
        format_label='Webpage',
        body_text='scraped full-page blob that must not be the redline source',
        rule_labels=['SEC: Rule 206(4)-1'],
        findings=findings,
        gap_summary='Homepage review',
        layout_notes=GRANITE_PEAK_LAYOUT_NOTES,
    )
    assert docx_has_track_changes(output) is True
    assert redline_bytes_have_rule_gap_fix(output) is True
    with ZipFile(BytesIO(output)) as archive:
        comments = archive.read('word/comments.xml').decode()
        document = archive.read('word/document.xml').decode()
    assert 'scraped full-page blob' not in document
    assert 'HERO' in document
    assert 'BADGE ROW' in document
    assert 'TESTIMONIAL CAROUSEL' in document
    assert 'MID-PAGE' in document
    assert 'FOOTER' in document
    assert 'Retire with confidence' in document
    assert 'net of fees' in document
    assert 'seeks to manage downside risk' in document
    assert 'Statements by current clients' in document or 'no compensation was provided' in document
    assert 'RULE:' in comments
    assert 'GAP:' in comments
    assert 'FIX:' in comments
    assert 'items 1–4 must sit adjacent' not in document
    assert 'Covered by items' not in document


def test_webpage_redline_applies_when_excerpt_is_placeholder_and_copy_has_newlines() -> None:
    """Scraped homepages often store multiline quotes and placeholder finding excerpts."""
    quote = (
        '"Hazel has been a game changer for our firm. It actually improves how we communicate '
        'our value to prospects, trains us to be better after every meeting, and even drafts '
        'the first version of our client plans."'
    )
    notes = [
        {
            'region': 'hero',
            'font_note': 'full-width banner, 48pt headline',
            'excerpt': (
                'The wealth platform built for independent advisors\n'
                'Save 13 weeks and thousands on software costs every year with Altruist.'
            ),
        },
        {
            'region': 'testimonial',
            'font_note': 'rotating, 24pt type',
            'excerpt': f'{quote}\n{quote}',
        },
        {
            'region': 'footer',
            'below_fold': True,
            'font_note': '7pt grey text on white, below the fold, after the sitemap',
            'excerpt': 'Altruist Financial LLC 3030 S La Cienega Blvd Culver City, CA 90232',
        },
    ]
    findings = [
        {
            'id': 'testimonial',
            'region': 'testimonial',
            'excerpt': 'Testimonial carousel',
            'title': 'Testimonial carousel',
            'rule': '206(4)-1',
            'why': 'A testimonial appears without adjacent client-status disclosures.',
            'recommended_option': 'add_disclosure',
            'selected_option': 'add_disclosure',
            'options': ['add_disclosure', 'redline', 'pull'],
            'draft_text': (
                'Statements by current clients; no compensation was provided. '
                'Individual results vary and are not typical.'
            ),
        },
        {
            'id': 'time_savings',
            'excerpt': 'Save 13 weeks and thousands on software costs',
            'title': 'Unsubstantiated time-savings claim',
            'rule': '206(4)-1',
            'why': 'Specific time and cost savings are performance-like claims that need support or qualification.',
            'recommended_option': 'redline',
            'selected_option': 'redline',
            'options': ['redline', 'pull'],
            'draft_text': '',
        },
    ]
    output = build_marketing_redline_bytes(
        title='Altruist homepage',
        format_label='Webpage',
        body_text='unused scraped blob',
        rule_labels=['SEC: Rule 206(4)-1'],
        findings=findings,
        gap_summary='Homepage review',
        layout_notes=notes,
    )
    assert docx_has_track_changes(output) is True
    assert redline_bytes_have_rule_gap_fix(output) is True
    with ZipFile(BytesIO(output)) as archive:
        comments = archive.read('word/comments.xml').decode()
        document = archive.read('word/document.xml').decode()
    assert 'w:ins' in document or 'w:del' in document
    assert 'Statements by current clients' in document
    assert 'Save 13 weeks' in document
    assert 'TESTIMONIAL CAROUSEL' in document
    assert 'RULE:' in comments
    assert 'GAP:' in comments


def _clause_value(right):
    return getattr(right, 'value', right)


class _MemQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._clauses = []

    def filter(self, *args):
        self._clauses.extend(args)
        return self

    def order_by(self, *args):
        del args
        return self

    def _ok(self, row) -> bool:
        for clause in self._clauses:
            key = getattr(getattr(clause, 'left', None), 'key', None)
            if not key:
                continue
            actual = getattr(row, key, None)
            expected = _clause_value(getattr(clause, 'right', None))
            operator = getattr(clause, 'operator', eq)
            if operator is ne:
                if actual == expected:
                    return False
            elif actual != expected:
                return False
        return True

    def one_or_none(self):
        matched = [row for row in self._rows if self._ok(row)]
        if len(matched) > 1:
            raise AssertionError('expected at most one row')
        return matched[0] if matched else None

    def first(self):
        matched = [row for row in self._rows if self._ok(row)]
        return matched[0] if matched else None


class _MemDb:
    def __init__(self, *, policies=None, contents=None):
        self.policies = list(policies or [])
        self.contents = list(contents or [])

    def query(self, model):
        if model is Policy:
            return _MemQuery(self.policies)
        if model is MarketingContent:
            return _MemQuery(self.contents)
        return _MemQuery([])

    def add(self, obj):
        if isinstance(obj, Policy):
            self.policies.append(obj)

    def flush(self):
        seen: set[tuple[str, str, int]] = set()
        for policy in self.policies:
            key = (policy.org_id, policy.name, int(policy.version or 1))
            if key in seen:
                raise IntegrityError('INSERT', {}, Exception('policies_org_name_version_key'))
            seen.add(key)

    def begin_nested(self):
        return nullcontext()

    def expunge(self, obj):
        if obj in self.policies:
            self.policies.remove(obj)


def _altruist_content(**overrides) -> SimpleNamespace:
    content = SimpleNamespace(
        id='content-new',
        org_id='org_internal_development',
        title='Altruist Homepage',
        name='Altruist Homepage',
        format='webpage',
        source_url='https://altruist.com',
    )
    content.__dict__.update(overrides)
    return content


def test_ensure_marketing_shell_reuses_orphaned_name_collision() -> None:
    content = _altruist_content()
    leftover = Policy(
        id='shell-old',
        org_id=content.org_id,
        name=marketing_shell_policy_name(content),
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
        file_url=marketing_shell_marker('deleted-content'),
    )
    db = _MemDb(policies=[leftover])

    policy = ensure_marketing_shell_policy(db, content)

    assert policy.id == 'shell-old'
    assert policy.file_url == marketing_shell_marker(content.id)
    assert len(db.policies) == 1


def test_ensure_marketing_shell_reuses_legacy_em_dash_name() -> None:
    content = _altruist_content()
    leftover = Policy(
        id='shell-dash',
        org_id=content.org_id,
        name='Webpage — Altruist Homepage (altruist.com)',
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
        file_url=marketing_shell_marker('deleted-content'),
    )
    db = _MemDb(policies=[leftover])

    policy = ensure_marketing_shell_policy(db, content)

    assert policy.id == 'shell-dash'
    assert policy.name == marketing_shell_policy_name(content)
    assert policy.name.startswith('Webpage: Altruist Homepage')


def test_ensure_marketing_shell_disambiguates_when_other_content_still_exists() -> None:
    content = _altruist_content(id='content-new')
    leftover = Policy(
        id='shell-live',
        org_id=content.org_id,
        name=marketing_shell_policy_name(content),
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
        file_url=marketing_shell_marker('content-live'),
    )
    live = SimpleNamespace(id='content-live', org_id=content.org_id)
    db = _MemDb(policies=[leftover], contents=[live])

    policy = ensure_marketing_shell_policy(db, content)

    assert policy.id != 'shell-live'
    assert policy.file_url == marketing_shell_marker(content.id)
    assert policy.name != leftover.name
    assert ' · ' in policy.name
    assert len(db.policies) == 2


def test_ensure_marketing_shell_recovers_from_unique_violation() -> None:
    content = _altruist_content()
    leftover = Policy(
        id='shell-raced',
        org_id=content.org_id,
        name=marketing_shell_policy_name(content),
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
        file_url=marketing_shell_marker('deleted-content'),
    )
    db = _MemDb()
    attempts = {'n': 0}

    def _flush():
        attempts['n'] += 1
        if attempts['n'] == 1:
            db.policies.append(leftover)
            raise IntegrityError('INSERT', {}, Exception('policies_org_name_version_key'))

    db.flush = _flush  # type: ignore[method-assign]

    policy = ensure_marketing_shell_policy(db, content)

    assert policy.id == 'shell-raced'
    assert policy.file_url == marketing_shell_marker(content.id)


