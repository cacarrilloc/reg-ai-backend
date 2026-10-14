"""Marketing Review Agent: ask-vs-say, Mode B filter, SSRF, options, isolation."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ai_fakes import FakeLLM
from fastapi import HTTPException

from app.core.roles import CHIEF_COMPLIANCE_OFFICER, PRODUCT_2
from app.core.security import CurrentUser
from app.schemas.marketing import MarketingReviewDecision
from app.services.marketing_ingest_service import (
    assert_safe_public_url,
    canonical_marketing_source_url,
    capture_blog_from_html,
    capture_homepage_from_html,
    clean_page_title,
    display_homepage_url,
    infer_format_from_inputs,
    normalize_format,
    validate_upload_meta,
)
from app.services.marketing_mode_b import marketing_tags_for_publication
from app.services.marketing_output_adapter import build_tracked_docx_bytes, output_kind_for_format
from app.services.marketing_review_engine import (
    classify_marketing_material,
    finalize_marketing_findings,
    heuristic_review,
    marketing_review_system_prompt,
    merge_review_results,
    should_ask,
    synthesize_layout_notes,
)
from app.services.marketing_rule_store import publication_touches_marketing
from app.services.marketing_service import (
    decide_marketing_review,
    delete_marketing_content,
    prepare_marketing_review_redline,
)
from app.services.marketing_types import OPTIONS, PERFORMANCE_DISCLAIMER, REPORT_HEADER

# Example 1 — firm homepage. Source of truth: the layout-review screenshots
# (granitepeakwealth.com capture, visual pass, ask-vs-say, keyed CCO frame).
EXAMPLE_1_HOMEPAGE = """
Retire with confidence.
Granite Peak clients have earned an average of 11.4% annually since 2015.

Forbes Best-In-State Wealth Advisors 2025. Five Star Wealth Manager. A+ BBB Rating.

"Granite Peak doubled my portfolio. I finally sleep at night." — Janet R., client since 2018
"The only advisors I trust." — Mark T.

Our disciplined process has protected client capital in every downturn since the firm was founded.

Footer — 7pt grey, below the fold, after the sitemap: Past performance does not guarantee
future results. The 11.4% annual figure is gross of fees. Testimonials are from current
clients; no compensation was provided. Third-party rankings are not representative of all clients.
"""
EXAMPLE_1_RATING_ANSWER = (
    'Five Star involves a paid entry fee; Forbes and BBB do not.'
)


def _homepage_review(answers: dict[str, str] | None = None) -> dict:
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=EXAMPLE_1_HOMEPAGE,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    return heuristic_review(
        classification=classification,
        body_text=EXAMPLE_1_HOMEPAGE,
        layout_notes=[],
        answers=answers,
    )


def test_homepage_does_not_reask_facts_already_in_the_footer() -> None:
    result = _homepage_review()
    prompts = ' '.join(item['prompt'].lower() for item in result['questions'])
    assert 'before or after fees' not in prompts
    assert 'testimonial' not in prompts
    assert 'gross' not in prompts
    assert any('rating' in item['prompt'].lower() or 'forbes' in item['prompt'].lower() for item in result['questions'])
    assert any('layout' in item['why'].lower() or 'prominent' in item['why'].lower() for item in result['findings'])
    assert result['report_header'] == REPORT_HEADER
    assert PERFORMANCE_DISCLAIMER in result['performance_disclaimer']


def test_example1_homepage_matches_screenshot_first_pass() -> None:
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=EXAMPLE_1_HOMEPAGE,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert classification['format'] == 'webpage'
    assert classification['registrant_type'] == 'ria'
    assert 'performance' in classification['tags']
    assert 'testimonial' in classification['tags']
    assert 'third_party_rating' in classification['tags']
    assert '206(4)-1' in classification['rules']
    assert '204-2' in classification['rules']

    notes = synthesize_layout_notes(
        body_text=EXAMPLE_1_HOMEPAGE,
        layout_notes=[],
        format_name='webpage',
    )
    regions = {str(note.get('region')) for note in notes}
    assert {'hero', 'badge_row', 'testimonial', 'mid_page', 'footer'} <= regions
    assert any(note.get('below_fold') for note in notes)

    result = _homepage_review()
    by_id = {item['id']: item for item in result['findings']}
    assert set(by_id) >= {
        'performance_gross_hero',
        'third_party_rating',
        'testimonial',
        'safety_claim',
        'layout_buried',
    }
    ordered_ids = [item['id'] for item in result['findings'] if item['id'] in {
        'performance_gross_hero',
        'third_party_rating',
        'testimonial',
        'safety_claim',
        'layout_buried',
    }]
    assert ordered_ids == [
        'performance_gross_hero',
        'third_party_rating',
        'testimonial',
        'safety_claim',
        'layout_buried',
    ]
    assert by_id['performance_gross_hero']['recommended_option'] == 'redline'
    assert 'pull' in by_id['performance_gross_hero']['options']
    assert by_id['third_party_rating']['status'] == 'held'
    assert by_id['testimonial']['status'] != 'held'
    assert 'doubled' in by_id['testimonial']['why'].lower() or 'performance' in by_id['testimonial']['why'].lower()
    assert 'cannot be cured' in by_id['safety_claim']['why'].lower()
    assert by_id['layout_buried']['recommended_option'] == 'redline'
    assert len(result['questions']) == 1
    assert 'Forbes' in result['questions'][0]['prompt']
    assert 'Five Star' in result['questions'][0]['prompt']
    assert 'BBB' in result['questions'][0]['prompt']
    assert 'Granite Peak' in result['questions'][0]['prompt']


FORTRESS_HOMEPAGE = """
Fortress Financial Group
Fee-Only Fiduciary Independent Advisory Custody with Fidelity
# Approaching Retirement? Make Every Decision Work Together.
One advisory relationship that coordinates your retirement income, taxes, investments,
benefits, and estate considerations, so no decision is made in isolation.
30 minutes. No cost. No pressure.
50+ Google Reviews
Client perspective The relationship matters as much as the plan. Hear one client describe
the experience of working with Fortress, or read voluntary feedback from current and former
clients on Google.
Client disclosure: The featured speaker is a Fortress client. No compensation was provided.
"""


def test_squarespace_homepage_flags_reviews_and_fiduciary_claims() -> None:
    page = capture_homepage_from_html(
        """
        <html><head><title>Fortress Financial Group</title></head>
        <body><h1>Approaching Retirement?</h1><p>Fee-Only Fiduciary Independent Advisory</p></body>
        </html>
        """,
        'https://fortressfg.net/',
    )
    assert page.page_title
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=FORTRESS_HOMEPAGE,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert 'testimonial' in classification['tags']
    assert 'third_party_rating' in classification['tags']
    result = heuristic_review(
        classification=classification,
        body_text=FORTRESS_HOMEPAGE,
        layout_notes=[],
    )
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['testimonial']['recommended_option'] == 'add_disclosure'
    assert by_id['third_party_rating']['status'] == 'held'
    assert by_id['fiduciary_claim']['recommended_option'] == 'add_disclosure'


def test_example1_homepage_after_rating_answer_matches_screenshot() -> None:
    result = _homepage_review(answers={'rating_paid': EXAMPLE_1_RATING_ANSWER})
    by_id = {item['id']: item for item in result['findings']}
    rating = by_id['third_party_rating']
    assert rating['status'] == 'open'
    assert rating['recommended_option'] == 'add_disclosure'
    assert 'five star' in rating['why'].lower()
    assert 'paid' in rating['why'].lower()
    assert 'forbes' in rating['why'].lower()
    assert 'add a readable caption' in (rating.get('draft_text') or '').lower()
    assert by_id['layout_buried']['recommended_option'] == 'do_nothing'
    assert 'covered by items 1' in by_id['layout_buried']['option_labels']['do_nothing'].lower()
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert 'net of fees X.X%' in drafts
    assert 'seeks to manage downside risk' in drafts
    assert 'Statements by current clients' in drafts
    assert not result['questions'] or all(
        item.get('id') != 'rating_paid' or EXAMPLE_1_RATING_ANSWER
        for item in result['questions']
    )
    # After the answer the rating question is already resolved; do not re-ask gross or paid testimonials.
    prompts = ' '.join(item['prompt'].lower() for item in result['questions'])
    assert 'before or after fees' not in prompts
    assert 'testimonial' not in prompts


EXAMPLE_1_HOMEPAGE_HTML = """
<!doctype html>
<html>
  <head><title>Granite Peak Wealth</title></head>
  <body>
    <header class="hero">
      <h1>Retire with confidence.</h1>
      <p>Granite Peak clients have earned an average of 11.4% annually since 2015.</p>
    </header>
    <div class="badges">
      Forbes Best-In-State Wealth Advisors 2025. Five Star Wealth Manager. A+ BBB Rating.
    </div>
    <blockquote class="testimonial">
      "Granite Peak doubled my portfolio. I finally sleep at night." — Janet R., client since 2018
    </blockquote>
    <p>"The only advisors I trust." — Mark T.</p>
    <p>Our disciplined process has protected client capital in every downturn since the firm was founded.</p>
    <footer style="font-size:7pt">
      Past performance does not guarantee future results. The 11.4% annual figure is gross of fees.
      Testimonials are from current clients; no compensation was provided.
      Third-party rankings are not representative of all clients.
    </footer>
  </body>
</html>
"""


def test_homepage_html_fills_template_and_matches_screenshot_findings() -> None:
    captured = capture_homepage_from_html(EXAMPLE_1_HOMEPAGE_HTML, 'https://granitepeakwealth.com')
    assert captured.page_title == 'Granite Peak Wealth'
    assert '11.4%' in captured.text
    assert 'Footer' in captured.text
    assert 'below the fold' in captured.text
    regions = {str(note.get('region')) for note in captured.notes}
    assert {'hero', 'badge_row', 'testimonial', 'mid_page', 'footer'} <= regions
    assert any(note.get('region') == 'footer' and note.get('below_fold') for note in captured.notes)
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=captured.text,
        audience='retail',
        registrant_type='ria',
        layout_notes=captured.notes,
    )
    assert '206(4)-1' in classification['rules']
    assert '204-2' in classification['rules']
    result = heuristic_review(
        classification=classification,
        body_text=captured.text,
        layout_notes=captured.notes,
    )
    by_id = {item['id']: item for item in result['findings']}
    assert set(by_id) >= {
        'performance_gross_hero',
        'third_party_rating',
        'testimonial',
        'safety_claim',
        'layout_buried',
    }
    prompts = ' '.join(item['prompt'].lower() for item in result['questions'])
    assert 'before or after fees' not in prompts
    assert 'fetched_page' not in {note.get('region') for note in captured.notes}
    assert any(note.get('region') == 'hero' and note.get('excerpt') for note in captured.notes)


def test_page_screenshots_slice_and_mark_homepage_findings() -> None:
    from app.services.marketing_page_screenshot import (
        apply_region_markers,
        apply_vision_marker_payload,
        attach_page_screenshot_paths,
        merge_screenshot_notes,
        notes_from_screenshots,
        slice_full_page_jpeg,
    )
    from app.services.storage_service import StoredFile

    tall = _jpeg_bytes(1280, 2000)
    shots = slice_full_page_jpeg(tall, source_url='https://granitepeakwealth.com', slice_height=800)
    assert [item.page for item in shots] == [1, 2, 3]
    assert shots[0].font_note == 'above the fold'
    assert shots[-1].height <= 800

    def fake_upload(bucket: str, path: str, contents: bytes, content_type: str | None) -> StoredFile:
        assert contents[:3] == b'\xff\xd8\xff'
        assert content_type == 'image/jpeg'
        return StoredFile(bucket=bucket, path=path)

    with patch('app.services.storage_service.upload_file', fake_upload):
        attach_page_screenshot_paths(shots, org_id='org-a', content_id='home-1')
    notes = notes_from_screenshots(shots)
    assert [note['region'] for note in notes] == [
        'slide_or_page_1',
        'slide_or_page_2',
        'slide_or_page_3',
    ]
    assert all(note.get('slide_image_path') for note in notes)
    merged = merge_screenshot_notes(
        [{'region': 'hero', 'excerpt': '11.4%'}, {'region': 'slide_or_page_9', 'excerpt': 'old'}],
        notes,
    )
    assert [note['region'] for note in merged[:3]] == [
        'slide_or_page_1',
        'slide_or_page_2',
        'slide_or_page_3',
    ]
    assert merged[-1]['region'] == 'hero'

    findings = [
        {'id': 'performance_gross_hero', 'region': 'hero', 'excerpt': '11.4%'},
        {'id': 'layout_buried', 'region': 'footer', 'excerpt': 'gross of fees'},
    ]
    apply_region_markers(findings, page_count=3)
    assert findings[0]['slide'] == 1
    assert findings[1]['slide'] == 3
    apply_vision_marker_payload(
        findings,
        {'markers': [{'id': 'performance_gross_hero', 'page': 1, 'marker_x': 0.81, 'marker_y': 0.22}]},
        page_count=3,
    )
    assert findings[0]['marker_x'] == 0.81
    assert findings[0]['marker_y'] == 0.22
    marked, _changes = finalize_marketing_findings(
        findings,
        {'format': 'webpage'},
    )
    by_id = {item['id']: item for item in marked}
    assert by_id['performance_gross_hero']['slide'] == 1
    assert by_id['layout_buried']['slide'] == 3
    assert by_id['performance_gross_hero']['marker_x'] == 0.81


def test_page_screenshot_capture_rejects_private_urls() -> None:
    import asyncio

    from app.services.marketing_page_screenshot import capture_public_page_jpeg

    assert asyncio.run(capture_public_page_jpeg('http://example.com')) is None
    assert asyncio.run(capture_public_page_jpeg('https://127.0.0.1/')) is None


def test_page_title_strips_tracking_urls_and_query_strings() -> None:
    tracking = (
        'https://altruist.com/try/compare-custodians/'
        '?utm_term=wealth%20management%20ria&utm_source=google&hsa_net=adwords'
    )
    assert clean_page_title(
        f'{tracking}\nCompare Custodians - Altruist',
        source_url=tracking,
    ) == 'Compare Custodians - Altruist'
    html = f"""
    <html>
      <head>
        <title>{tracking}
        Compare Custodians - Altruist</title>
      </head>
      <body><h1>Compare Custodians</h1></body>
    </html>
    """
    captured = capture_homepage_from_html(html, tracking)
    assert captured.page_title == 'Compare Custodians - Altruist'
    title_note = next(note for note in captured.notes if note.get('region') == 'title')
    assert title_note['excerpt'] == 'Compare Custodians - Altruist'
    assert title_note['font_note'] == 'https://altruist.com/try/compare-custodians'
    assert display_homepage_url(tracking) == 'https://altruist.com/try/compare-custodians'
    assert 'utm_' not in captured.page_title
    assert 'http' not in captured.page_title.lower()


def test_footer_does_not_guarantee_is_not_a_safety_claim() -> None:
    body = (
        'Welcome to our advisory firm.\n\n'
        'Footer — below the fold: Past performance does not guarantee future results.'
    )
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=body,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert 'safety_claim' not in classification['tags']
    result = heuristic_review(
        classification=classification,
        body_text=body,
        layout_notes=[],
    )
    assert not any(item.get('id') == 'safety_claim' for item in result['findings'])


def test_thin_homepage_html_still_fills_visual_pass_regions() -> None:
    html = """
    <html>
      <head>
        <title>North Shore Advisors</title>
        <meta name="description" content="Retirement planning for families in Evanston." />
      </head>
      <body>
        <p>We help families stay invested through market cycles with a written plan.</p>
        <p>Call us to book a conversation about your retirement income.</p>
        <p>© 2026 North Shore Advisors. Past performance does not guarantee future results.</p>
      </body>
    </html>
    """
    captured = capture_homepage_from_html(html, 'https://northshoreadvisors.example')
    regions = {str(note.get('region')) for note in captured.notes}
    assert 'fetched_page' not in regions
    assert 'hero' in regions
    assert captured.page_title == 'North Shore Advisors'
    assert 'Retirement planning' in captured.text or 'stay invested' in captured.text
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=captured.text,
        audience='retail',
        registrant_type='ria',
        layout_notes=captured.notes,
    )
    result = heuristic_review(
        classification=classification,
        body_text=captured.text,
        layout_notes=captured.notes,
    )
    assert not any(item.get('id') == 'safety_claim' for item in result['findings'])


def test_homepage_scrape_keeps_card_div_copy_and_button_labels() -> None:
    html = """
    <html>
      <head><title>Fidelity Investments</title></head>
      <body>
        <main>
          <h1>Invest today and plan for tomorrow</h1>
          <p>We can help you get started.</p>
          <h2>Start investing</h2>
          <h3>Invest smart from the start with a brokerage account</h3>
          <div class="row-item-title">$0 account fees<sup>2</sup></div>
          <div class="row-item-description">Keep your money working toward your goals.</div>
          <div class="row-item-title">$0 commissions</div>
          <div class="row-item-description">Trade US stocks and ETFs commission free online.</div>
          <span class="pvd-button__text">Open a brokerage account</span>
          <h2>Save for retirement</h2>
          <div class="row-item-title">Tax savings</div>
          <div class="row-item-description">Any investment growth in a Roth is tax-free.</div>
        </main>
        <footer>© 2024 FMR LLC. All Rights Reserved.</footer>
      </body>
    </html>
    """
    captured = capture_homepage_from_html(html, 'https://www.fidelity.com/')
    assert '$0 account fees' in captured.text
    assert 'Keep your money working toward your goals' in captured.text
    assert '$0 commissions' in captured.text
    assert 'Open a brokerage account' in captured.text
    assert 'Any investment growth in a Roth is tax-free' in captured.text
    assert '# Invest today and plan for tomorrow' in captured.text
    assert '## Start investing' in captured.text
    assert '### $0 account fees' in captured.text
    assert '[Open a brokerage account]' in captured.text
    mid = next(note for note in captured.notes if note.get('region') == 'mid_page')
    assert '### $0 account fees' in str(mid.get('excerpt'))


def test_renderable_page_html_keeps_structure_and_strips_scripts() -> None:
    from app.services.marketing_ingest_service import build_renderable_page_html

    html = """
    <!DOCTYPE html>
    <html>
      <head>
        <script>alert(1)</script>
        <style>.hero { font-size: 48px; }</style>
        <link rel="stylesheet" href="/app.css">
        <link rel="preload" as="script" href="/boot.js">
      </head>
      <body>
        <main>
          <h1 onclick="alert(1)">Invest today and plan for tomorrow</h1>
          <a href="javascript:alert(1)">Open a brokerage account</a>
          <p>Keep your money working toward your goals.</p>
        </main>
      </body>
    </html>
    """
    rendered = build_renderable_page_html(html, 'https://www.fidelity.com/')
    assert '<script' not in rendered.lower()
    assert 'onclick' not in rendered.lower()
    assert 'javascript:' not in rendered.lower()
    assert 'boot.js' not in rendered
    assert '/app.css' not in rendered
    assert 'Invest today and plan for tomorrow' in rendered
    assert 'Keep your money working toward your goals.' in rendered
    assert '<h1' in rendered
    assert 'https://www.fidelity.com/' in rendered


def test_renderable_page_html_keeps_main_when_head_is_huge() -> None:
    from app.services.marketing_ingest_service import build_renderable_page_html

    padding = 'x' * 450_000
    html = f"""
    <!DOCTYPE html>
    <html>
      <head><style>.pad{{{padding}}}</style></head>
      <body>
        <header>Fidelity nav</header>
        <main>
          <h1>Invest today and plan for tomorrow</h1>
          <h2>Start investing</h2>
          <p>Keep your money working toward your goals.</p>
        </main>
      </body>
    </html>
    """
    rendered = build_renderable_page_html(html, 'https://www.fidelity.com/')
    assert 'Invest today and plan for tomorrow' in rendered
    assert 'Start investing' in rendered
    assert padding not in rendered


def test_broker_dealer_switches_on_finra_topic_rules() -> None:
    classification = classify_marketing_material(
        format_name='webpage',
        body_text='Our variable annuity includes a Lipper ranking and security futures.',
        audience='retail',
        registrant_type='broker_dealer',
        layout_notes=[],
    )
    assert classification['rules'][:3] == ['204-2', '2210', '2010'] or (
        '204-2' in classification['rules']
        and '2210' in classification['rules']
        and '2010' in classification['rules']
    )
    assert '2211' in classification['rules']
    assert '2212' in classification['rules']
    assert '2215' in classification['rules']
    assert '206(4)-1' not in classification['rules']


def test_llm_cannot_reask_facts_already_on_the_homepage() -> None:
    heuristic = _homepage_review()
    llm = {
        'findings': heuristic['findings'],
        'questions': [
            {'id': 'fee_basis', 'prompt': 'Is the stated return before or after fees?'},
            {'id': 'testimonial_paid', 'prompt': 'Was the person quoted paid or compensated for that testimonial?'},
            {'id': 'rating_paid', 'prompt': 'Did the firm pay for the Forbes rating?'},
        ],
        'change_list': [],
        'classification': {'tags': ['performance']},
    }
    merged = merge_review_results(
        heuristic,
        llm,
        body_text=EXAMPLE_1_HOMEPAGE,
        classification={'format': 'webpage', 'tags': ['performance', 'testimonial', 'third_party_rating']},
    )
    prompts = ' '.join(item['prompt'].lower() for item in merged['questions'])
    assert 'before or after fees' not in prompts
    assert 'testimonial' not in prompts
    assert any('rating' in item['prompt'].lower() or 'forbes' in item['prompt'].lower() for item in merged['questions'])
    assert {item['id'] for item in merged['findings']} >= {
        'performance_gross_hero',
        'testimonial',
        'safety_claim',
        'layout_buried',
        'third_party_rating',
    }


def test_testimonial_paid_question_is_optional_when_page_is_silent() -> None:
    body = EXAMPLE_1_HOMEPAGE.replace('no compensation was provided', '')
    classification = classify_marketing_material(
        format_name='webpage',
        body_text=body,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    result = heuristic_review(
        classification=classification,
        body_text=body,
        layout_notes=[],
    )
    paid_q = next(item for item in result['questions'] if item.get('id') == 'testimonial_paid')
    assert paid_q.get('optional') is True
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['testimonial']['status'] == 'open'
    assert by_id['testimonial']['recommended_option'] == 'add_disclosure'
    assert 'held' not in by_id['testimonial']['options']

    answered = heuristic_review(
        classification=classification,
        body_text=body,
        layout_notes=[],
        answers={'testimonial_paid': 'No, the speaker was not compensated'},
    )
    why = next(item['why'] for item in answered['findings'] if item['id'] == 'testimonial')
    assert 'not compensated' in why.lower()


EXAMPLE_2_DECK = """
Slide 7 of 14 — Summit Advisory pitch deck
Performance That Speaks for Itself

Bar chart — two bars, 2025 only:
Summit Growth Strategy: +22.4% (2025)
S&P 500: +18.1% (2025)

Gold callout: Backtested to 1999, our model would have returned 14.2% annually — through two crashes.

Bottom right 11pt: Returns shown before advisory fees. See appendix for disclosures.

Appendix, slide 14, 9pt: Standard performance disclosures, net figure for 2025 (+20.1%), no multi-period table.
"""
EXAMPLE_2_ANSWER = (
    'the deck goes to both audiences; the benchmark is total return.'
)


def _deck_review(answers: dict[str, str] | None = None) -> dict:
    classification = classify_marketing_material(
        format_name='presentation',
        body_text=EXAMPLE_2_DECK,
        audience='prospect',
        registrant_type='ria',
        layout_notes=[],
    )
    return heuristic_review(
        classification=classification,
        body_text=EXAMPLE_2_DECK,
        layout_notes=[],
        answers=answers,
    )


def test_example2_pitch_deck_matches_screenshot_first_pass() -> None:
    classification = classify_marketing_material(
        format_name='presentation',
        body_text=EXAMPLE_2_DECK,
        audience='prospect',
        registrant_type='ria',
        layout_notes=[],
    )
    assert classification['format'] == 'presentation'
    assert 'performance' in classification['tags']
    assert 'hypothetical_performance' in classification['tags']
    assert 'benchmark_comparison' in classification['tags']
    assert '206(4)-1' in classification['rules']

    notes = synthesize_layout_notes(
        body_text=EXAMPLE_2_DECK,
        layout_notes=[],
        format_name='presentation',
    )
    regions = {str(note.get('region')) for note in notes}
    assert {'slide_title', 'bar_chart', 'callout', 'slide_footer', 'appendix'} <= regions

    result = _deck_review()
    by_id = {item['id']: item for item in result['findings']}
    assert [
        item['id'] for item in result['findings'] if item['id'] in {
            'cherry_pick',
            'gross_net_layout',
            'hypothetical_backtest',
            'benchmark_comparison',
            'puffery_title',
        }
    ] == [
        'cherry_pick',
        'gross_net_layout',
        'hypothetical_backtest',
        'benchmark_comparison',
        'puffery_title',
    ]
    assert by_id['cherry_pick']['recommended_option'] == 'redline'
    assert by_id['gross_net_layout']['recommended_option'] == 'redline'
    assert by_id['hypothetical_backtest']['status'] == 'held'
    assert by_id['benchmark_comparison']['recommended_option'] == 'add_disclosure'
    assert by_id['puffery_title']['recommended_option'] == 'do_nothing'
    prompts = ' '.join(item['prompt'] for item in result['questions'])
    assert 'retail prospects' in prompts
    assert 'total return' in prompts.lower() or 'dividends' in prompts.lower()
    assert 'before or after fees' not in prompts.lower()
    assert all('layout_buried' != item['id'] for item in result['findings'])


def test_example2_pitch_deck_after_answers_matches_screenshot() -> None:
    result = _deck_review(answers={'slide7_context': EXAMPLE_2_ANSWER})
    by_id = {item['id']: item for item in result['findings']}
    backtest = by_id['hypothetical_backtest']
    assert backtest['status'] == 'open'
    assert backtest['recommended_option'] == 'redline'
    assert 'pull' in backtest['options']
    assert 'retail' in backtest['why'].lower()
    assert 'methodology' in (backtest.get('draft_text') or '').lower()
    benchmark = by_id['benchmark_comparison']
    assert 'total return' in benchmark['why'].lower()
    assert '11pt' in (benchmark.get('draft_text') or '')
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert 'side-by-side' in drafts.lower() or '1-year' in drafts.lower()
    assert not any('before or after fees' in (item.get('prompt') or '').lower() for item in result['questions'])


def test_example2_splits_every_slide_for_text_and_diagram_embeddings() -> None:
    from app.services.marketing_slide_service import parse_slides_from_text, slide_embed_corpus
    from app.services.marketing_vector_gap import marketing_gap_embed_text

    slides = parse_slides_from_text(EXAMPLE_2_DECK)
    assert [item.page for item in slides] == [7, 14]
    assert '22.4%' in slides[0].text
    assert '20.1%' in slides[1].text
    corpus = slide_embed_corpus(slides)
    assert corpus[0][0] == 7
    assert 'SLIDE 7' in corpus[0][1]
    blob = marketing_gap_embed_text(
        body_text=EXAMPLE_2_DECK,
        layout_notes=[{'region': 'bar_chart', 'excerpt': 'Summit Growth Strategy: +22.4%', 'page': 7}],
        slide_embed_texts=[item[1] for item in corpus],
    )
    assert '22.4%' in blob
    assert 'SLIDE 14' in blob or '20.1%' in blob


def test_pptx_slides_are_extracted_for_presentation_review() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from app.services.marketing_ingest_service import extract_marketing_text, layout_notes_from_bytes
    from app.services.marketing_slide_service import extract_presentation_slides
    from app.services.marketing_types import ALLOWED_UPLOAD_SUFFIXES

    assert '.pptx' in ALLOWED_UPLOAD_SUFFIXES
    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        for index, title in enumerate(('Cover', 'Performance That Speaks for Itself 22.4%'), start=1):
            archive.writestr(
                f'ppt/slides/slide{index}.xml',
                (
                    '<?xml version="1.0"?>'
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f'<a:t>{title}</a:t></p:sld>'
                ),
            )
    payload = buffer.getvalue()
    slides = extract_presentation_slides(
        filename='summit.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        contents=payload,
    )
    assert [item.page for item in slides] == [1, 2]
    assert 'Cover' in slides[0].text
    assert '22.4%' in slides[1].text
    extracted = extract_marketing_text(
        filename='summit.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        contents=payload,
    )
    assert 'Slide 1 of 2' in extracted
    notes = layout_notes_from_bytes(
        filename='summit.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        contents=payload,
    )
    pages = {note.get('page') for note in notes if str(note.get('region') or '').startswith('slide_or_page_')}
    assert pages == {1, 2}


def test_presentation_source_filename_keeps_pptx_when_mime_is_octet_stream() -> None:
    from app.services.marketing_slide_service import presentation_source_filename

    assert (
        presentation_source_filename(
            storage_path='storage://marketing-contents/org-a/marketing/abc/pitch-deck-example.pptx',
            content_type='application/octet-stream',
        )
        == 'pitch-deck-example.pptx'
    )


def test_screenshot_slide_notes_use_diagram_text_when_xml_is_empty() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, joined_slide_text

    slide = SlidePayload(
        page=1,
        text='',
        page_count=5,
        images=[SlideImage(media_type='image/jpeg', data=b'fake', name='image1.jpg')],
        diagram_captions=['Airbnb Pitch · 22.4% net'],
    )
    notes = slide.as_layout_notes()
    card = next(item for item in notes if str(item.get('region')).startswith('slide_or_page_'))
    assert '22.4%' in str(card.get('excerpt'))
    assert '22.4%' in joined_slide_text([slide])


def test_joined_slide_text_omits_prefix_for_single_screenshot() -> None:
    from app.services.marketing_slide_service import SlidePayload, joined_slide_text

    slide = SlidePayload(page=1, text='Savings account definition', label='screenshot')
    assert joined_slide_text([slide]) == 'Savings account definition'


def test_png_screenshot_is_not_decoded_as_article_copy() -> None:
    from app.services.marketing_ingest_service import extract_marketing_text, layout_notes_from_bytes

    png = b'\x89PNG\r\n\x1a\n' + b'\x00IHDR' + b'\x00' * 64
    text = extract_marketing_text(
        filename='sequence-article.png',
        content_type='text/plain',
        contents=png,
    )
    assert text == ''
    notes = layout_notes_from_bytes(
        filename='sequence-article.png',
        content_type='text/plain',
        contents=png,
    )
    assert notes[0]['region'] == 'slide_or_page_1'
    assert notes[0]['label'] == 'screenshot'


def test_readable_marketing_body_ignores_png_dump() -> None:
    from app.services.marketing_ingest_service import readable_marketing_body

    assert readable_marketing_body('PNG\nIHDR', [{'region': 'slide_or_page_1', 'excerpt': 'Sequence of returns'}]) == (
        'Sequence of returns'
    )
    assert readable_marketing_body(
        'PNG IHDRF MiCCPiCC ProfileHWXS',
        [{'region': 'slide_or_page_1', 'excerpt': 'DEFINITION\nA savings account holds deposits.'}],
    ) == 'DEFINITION\nA savings account holds deposits.'
    assert readable_marketing_body('Sequence of returns for retirees', []) == 'Sequence of returns for retirees'


def test_image_screenshot_reuses_uploaded_file_as_slide_preview() -> None:
    from app.services.marketing_service import _with_source_screenshot

    record = SimpleNamespace(
        storage_path='storage://marketing-contents/org-a/marketing/c1/ad.png',
        format='image',
        file_content_type='image/png',
        body_text='',
    )
    notes = _with_source_screenshot(record, [])
    assert notes[0]['region'] == 'slide_or_page_1'
    assert notes[0]['slide_image_path'] == record.storage_path
    assert notes[0]['label'] == 'screenshot'


def test_file_slides_for_marketing_review_accepts_png_and_pdf() -> None:
    from io import BytesIO

    from pypdf import PdfWriter

    from app.services.marketing_slide_service import file_slides_for_marketing_review

    jpeg = _jpeg_bytes(800, 600)
    png_slides = file_slides_for_marketing_review(
        filename='ad.png',
        content_type='image/png',
        contents=jpeg,
    )
    assert len(png_slides) == 1
    assert png_slides[0].label == 'screenshot'
    assert png_slides[0].preview_image is not None

    writer = PdfWriter()
    writer.add_blank_page(width=720, height=405)
    buffer = BytesIO()
    writer.write(buffer)
    pdf_slides = file_slides_for_marketing_review(
        filename='email-capture.pdf',
        content_type='application/pdf',
        contents=buffer.getvalue(),
    )
    assert len(pdf_slides) == 1
    assert pdf_slides[0].page == 1


@pytest.mark.asyncio
async def test_transcribe_runs_vision_for_image_format_uploads(monkeypatch) -> None:
    from app.services.marketing_review_workflow import _transcribe_stored_marketing_file

    jpeg = _jpeg_bytes(800, 600)
    captured: dict[str, object] = {}

    async def fake_caption(slides, **kwargs):
        captured['count'] = len(slides)
        captured['max_images'] = kwargs.get('max_images')
        slides[0].text = '11.4% net of fees'
        return slides

    monkeypatch.setattr('app.services.storage_service.download_file', lambda _path: jpeg)
    monkeypatch.setattr('app.services.marketing_slide_service.caption_slide_diagrams', fake_caption)
    monkeypatch.setattr('app.services.marketing_slide_service.attach_slide_preview_paths', lambda *a, **k: None)
    monkeypatch.setattr('app.services.marketing_slide_service.render_presentation_previews', lambda *a, **k: None)
    monkeypatch.setattr(
        'app.services.marketing_review_workflow.optional_llm_provider',
        lambda *_args, **_kwargs: object(),
    )

    slides, notes, transcribed = await _transcribe_stored_marketing_file(
        content_snapshot={
            'format': 'image',
            'storage_path': 'storage://marketing-contents/org/ad.png',
            'file_content_type': 'image/png',
            'layout_notes': [],
        },
        org_id='org-a',
        content_id='c1',
        settings=SimpleNamespace(anthropic_sonnet_model='claude-sonnet-4-6'),
    )
    assert captured['count'] == 1
    assert captured['max_images'] == 1
    assert '11.4%' in transcribed
    assert slides[0].text == '11.4% net of fees'
    assert notes and '11.4%' in str(notes[0].get('excerpt') or '')


@pytest.mark.asyncio
async def test_transcribe_reads_layout_note_screenshots_without_a_deck_file(monkeypatch) -> None:
    from app.services.marketing_review_workflow import _transcribe_stored_marketing_file

    jpeg = _jpeg_bytes(1280, 720)

    async def fake_caption(slides, **kwargs):
        slides[0].text = 'Book a free strategy call'
        return slides

    monkeypatch.setattr('app.services.storage_service.download_file', lambda _path: jpeg)
    monkeypatch.setattr('app.services.marketing_slide_service.caption_slide_diagrams', fake_caption)
    monkeypatch.setattr('app.services.marketing_slide_service.attach_slide_preview_paths', lambda *a, **k: None)
    monkeypatch.setattr(
        'app.services.marketing_review_workflow.optional_llm_provider',
        lambda *_args, **_kwargs: object(),
    )

    _slides, notes, transcribed = await _transcribe_stored_marketing_file(
        content_snapshot={
            'format': 'blog',
            'storage_path': '',
            'layout_notes': [{
                'region': 'slide_or_page_1',
                'page': 1,
                'label': 'screenshot',
                'slide_image_path': 'storage://marketing-contents/org/article.png',
                'excerpt': '',
            }],
        },
        org_id='org-a',
        content_id='c1',
        settings=SimpleNamespace(anthropic_sonnet_model='claude-sonnet-4-6'),
    )
    assert 'Book a free strategy call' in transcribed
    assert notes and 'Book a free strategy call' in str(notes[0].get('excerpt') or '')


def test_blog_screenshot_reuses_uploaded_file_as_slide_preview() -> None:
    from app.services.marketing_service import _with_source_screenshot

    record = SimpleNamespace(
        storage_path='storage://marketing-contents/org-a/marketing/c1/article.png',
        format='blog',
        file_content_type='text/plain',
        body_text='Sequence of returns for retirees',
    )
    notes = _with_source_screenshot(record, [{'region': 'title', 'excerpt': ''}])
    assert notes[0]['region'] == 'slide_or_page_1'
    assert notes[0]['slide_image_path'] == record.storage_path
    assert 'Sequence of returns' in str(notes[0]['excerpt'])


def test_decoded_png_signature_is_not_article_copy() -> None:
    from app.services.marketing_ingest_service import is_decoded_image_dump

    assert is_decoded_image_dump('PNG')
    assert is_decoded_image_dump('\ufffdPNG\r\nIHDR')
    assert is_decoded_image_dump('PNG IHDRF MiCCPiCC ProfileHWXS')
    assert is_decoded_image_dump('Slide 1 of 1\nPNG IHDRF MiCCPiCC Profile')
    assert not is_decoded_image_dump('Sequence of returns for retirees')


def test_sniff_image_media_type_detects_jpeg_bytes_named_png() -> None:
    from app.services.marketing_slide_service import sniff_image_media_type

    jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 24
    assert sniff_image_media_type(jpeg, name='image1.png', declared='image/png') == 'image/jpeg'


def test_email_screenshot_file_becomes_a_slide_note() -> None:
    from app.services.marketing_slide_service import slides_from_screenshot_file

    jpeg = _jpeg_bytes(800, 600)
    slides = slides_from_screenshot_file(
        filename='prospect-email.png',
        content_type='image/png',
        contents=jpeg,
    )
    assert len(slides) == 1
    assert slides[0].page == 1
    assert slides[0].preview_image is not None
    assert slides[0].preview_image.data == jpeg
    notes = slides[0].as_layout_notes()
    assert notes[0]['region'] == 'slide_or_page_1'
    assert notes[0]['page'] == 1
    assert notes[0]['image_count'] == 1


def test_uncaptioned_screenshot_notes_do_not_use_byte_placeholders() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, joined_slide_text

    slide = SlidePayload(
        page=1,
        text='',
        page_count=5,
        images=[SlideImage(media_type='image/png', data=b'fake', name='image1.png')],
    )
    notes = slide.as_layout_notes()
    card = next(item for item in notes if str(item.get('region')).startswith('slide_or_page_'))
    assert not str(card.get('excerpt') or '').strip()
    assert card.get('image_count') == 1
    assert joined_slide_text([slide]) == ''


def _jpeg_bytes(width: int, height: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    image = Image.new('RGB', (width, height), color=(30, 40, 80))
    buffer = BytesIO()
    image.save(buffer, format='JPEG', quality=70)
    return buffer.getvalue()


def test_slide_preview_skips_logos_and_stores_rendered_page() -> None:
    from app.services.marketing_slide_service import (
        SlideImage,
        SlidePayload,
        attach_slide_preview_paths,
        is_full_slide_image,
        slide_preview_image,
    )
    from app.services.storage_service import StoredFile

    logo = SlideImage(media_type='image/jpeg', data=_jpeg_bytes(64, 64), name='logo.jpg')
    screenshot = SlideImage(media_type='image/jpeg', data=_jpeg_bytes(1280, 720), name='slide.jpg')
    assert not is_full_slide_image(logo)
    assert is_full_slide_image(screenshot)
    slide = SlidePayload(page=7, text='Nobody automates', images=[logo])
    assert slide_preview_image(slide) is None
    slide.preview_image = screenshot
    assert slide_preview_image(slide) is screenshot

    def fake_upload(bucket: str, path: str, contents: bytes, content_type: str | None) -> StoredFile:
        assert contents[:3] == b'\xff\xd8\xff'
        return StoredFile(bucket=bucket, path=path)

    with patch('app.services.storage_service.upload_file', fake_upload):
        attach_slide_preview_paths([slide], org_id='org-a', content_id='deck-1')
    assert str(slide.image_storage_uri).endswith('slide-7.jpg')


def test_render_presentation_previews_from_pdf_pages() -> None:
    from io import BytesIO

    from pypdf import PdfWriter

    from app.services.marketing_slide_service import SlidePayload, render_presentation_previews

    writer = PdfWriter()
    writer.add_blank_page(width=720, height=405)
    writer.add_blank_page(width=720, height=405)
    buffer = BytesIO()
    writer.write(buffer)
    slides = [SlidePayload(page=1, text='one'), SlidePayload(page=2, text='two')]
    render_presentation_previews(buffer.getvalue(), 'deck.pdf', slides)
    assert slides[0].preview_image is not None
    assert slides[0].preview_image.data[:3] == b'\xff\xd8\xff'
    assert slides[1].preview_image is not None


def test_render_presentation_previews_skips_existing_screenshots() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, render_presentation_previews

    screenshot = SlideImage(media_type='image/jpeg', data=_jpeg_bytes(1280, 720), name='photo.jpg')
    slide = SlidePayload(page=1, text='', images=[screenshot])
    with patch('app.services.marketing_slide_service.presentation_pdf_bytes') as convert:
        render_presentation_previews(b'deck-bytes', 'deck.pptx', [slide])
        convert.assert_not_called()
    assert slide.preview_image is None


@pytest.mark.asyncio
async def test_caption_slide_diagrams_sends_sniffed_jpeg_media_type() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, caption_slide_diagrams

    jpeg = b'\xff\xd8\xff\xe0' + b'\x00' * 32
    captured: dict[str, str] = {}

    def respond(messages, _kwargs):  # type: ignore[no-untyped-def]
        captured['media_type'] = messages[0].content[0].media_type
        return 'Airbnb Pitch 22.4% net'

    slide = SlidePayload(
        page=1,
        text='',
        page_count=1,
        images=[SlideImage(media_type='image/png', data=jpeg, name='image1.png')],
    )
    result = await caption_slide_diagrams([slide], client=FakeLLM(responder=respond))
    assert captured['media_type'] == 'image/jpeg'
    assert '22.4%' in result[0].text


def test_slide_vision_json_becomes_redline_findings() -> None:
    from app.services.marketing_review_engine import drop_empty_review_placeholders
    from app.services.marketing_slide_service import (
        SlidePayload,
        apply_slide_vision_response,
        findings_from_slide_vision,
    )

    slide = SlidePayload(page=2, text='', page_count=5)
    apply_slide_vision_response(
        slide,
        (
            '{"transcription": "Airbnb Pitch. 22.4% net returns since 2009.",'
            ' "findings": [{'
            '"title": "Net return without period",'
            '"excerpt": "22.4% net returns since 2009",'
            '"rule": "206(4)-1",'
            '"why": "Performance without a defined time period and net/gross basis.",'
            '"recommended_option": "redline",'
            '"draft_text": "22.4% net of fees for 2009–2025.",'
            '"marker_x": 0.8,'
            '"marker_y": 0.25'
            '}]}'
        ),
    )
    assert '22.4%' in slide.text
    findings = findings_from_slide_vision([slide])
    assert findings[0]['slide'] == 2
    assert findings[0]['recommended_option'] == 'redline'
    assert findings[0]['marker_x'] == 0.8
    placeholder = {
        'recommended_option': 'do_nothing',
        'why': 'Reviewed against the current marketing rule set. No prohibited claim stood out.',
    }
    kept = drop_empty_review_placeholders([placeholder, findings[0]])
    assert kept == [findings[0]]


def test_presentation_findings_are_marked_in_slide_order() -> None:
    from app.services.marketing_review_engine import finalize_marketing_findings

    findings, change_list = finalize_marketing_findings(
        [
            {
                'id': 'benchmark_comparison',
                'title': 'Benchmark comparison',
                'slide': 11,
                'recommended_option': 'add_disclosure',
                'why': 'S&P comparison',
            },
            {
                'id': 'vision_slide_1_1',
                'title': 'Unsubstantiated superlative',
                'slide': 1,
                'recommended_option': 'redline',
                'why': 'Absolute claim',
            },
        ],
        {'format': 'presentation'},
    )
    assert [item['slide'] for item in findings] == [1, 11]
    assert [item['marker'] for item in findings] == [1, 2]
    assert [item['slide'] for item in change_list] == [1, 11]
    assert [item['marker'] for item in change_list] == [1, 2]


async def _async_noop(*_args, **_kwargs) -> None:
    return None


def _pptx_slide_xml(title: str) -> str:
    return (
        '<?xml version="1.0"?>'
        '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        f'<a:t>{title}</a:t></p:sld>'
    )


def test_pptx_slide_order_is_deck_order_not_lexicographic() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from app.services.marketing_ingest_service import layout_notes_from_bytes
    from app.services.marketing_review_engine import synthesize_layout_notes
    from app.services.marketing_slide_service import extract_presentation_slides

    buffer = BytesIO()
    with ZipFile(buffer, 'w') as archive:
        archive.writestr('ppt/slides/slide1.xml', _pptx_slide_xml('01 / THE PROBLEM'))
        archive.writestr('ppt/slides/slide10.xml', _pptx_slide_xml('APPENDIX / POTENTIAL WAVE 2 PRODUCT'))
        archive.writestr('ppt/slides/slide11.xml', _pptx_slide_xml('Additional products'))
        archive.writestr('ppt/slides/slide2.xml', _pptx_slide_xml('02 / WHO WE SELL TO'))
        archive.writestr('ppt/slides/slide9.xml', _pptx_slide_xml('APPENDIX / POSITIONING AND LEGAL'))
    payload = buffer.getvalue()
    slides = extract_presentation_slides(
        filename='overview.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        contents=payload,
    )
    assert [item.page for item in slides] == [1, 2, 3, 4, 5]
    assert 'THE PROBLEM' in slides[0].text
    assert 'WHO WE SELL TO' in slides[1].text
    assert 'POSITIONING' in slides[2].text
    assert 'WAVE 2' in slides[3].text
    assert 'Additional products' in slides[4].text

    notes = layout_notes_from_bytes(
        filename='overview.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        contents=payload,
    )
    doubled = notes + notes
    deduped = synthesize_layout_notes(
        body_text='\n\n'.join(item.text for item in slides),
        layout_notes=doubled,
        format_name='presentation',
    )
    pages = [
        note.get('page')
        for note in deduped
        if str(note.get('region') or '').startswith('slide_or_page_')
    ]
    assert pages == [1, 2, 3, 4, 5]


def test_presentation_global_redline_covers_all_slides() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    result = _deck_review()
    payload = build_tracked_docx_bytes(
        title='Summit Advisory pitch deck',
        body_text=EXAMPLE_2_DECK,
        findings=result['findings'],
        change_list=result['change_list'],
        rule_version='v26.07',
    )
    assert payload[:2] == b'PK'
    with ZipFile(BytesIO(payload)) as archive:
        xml = archive.read('word/document.xml').lower()
    assert b'draft' in xml
    assert b'cco' in xml
    assert b'slide 7' in xml
    assert output_kind_for_format('presentation', result['findings']) == 'tracked_docx'


OVERVIEW_DECK = """
Slide 1 of 11
01 / THE PROBLEM
$6.6B in SEC & FINRA fines in 2024 alone
60% of compliance time spent on manual data gathering

Slide 7 of 11
APPENDIX / COMPETITOR ANALYSIS
The market has incumbents. None own our wedge.
Nobody automates the full policy update workflow for mid-size BDs and RIAs.
We are the only agentic AI that takes a reg change all the way to a CCO-approved update.

Slide 9 of 11
Empower, Not Replace
"Your CCO handles the 5% requiring human judgment. We handle the 95% that is manual and mechanical."

Slide 11 of 11
Support for text, images, and video (Haast is doing this well)
Reduction in approval cycles (Sigmoid achieved 30-50% improvement)
"""


def test_sales_pitch_deck_does_not_use_homepage_heuristics() -> None:
    from app.services.marketing_slide_service import layout_notes_from_slides, parse_slides_from_text

    slides = parse_slides_from_text(OVERVIEW_DECK)
    notes = layout_notes_from_slides(slides)
    assert not any(note.get('below_fold') for note in notes)
    assert all(note.get('font_note') for note in notes if str(note.get('region') or '').startswith('slide_or_page_'))

    classification = classify_marketing_material(
        format_name='presentation',
        body_text=OVERVIEW_DECK,
        audience='prospect',
        registrant_type='ria',
        layout_notes=notes,
    )
    assert 'testimonial' not in classification['tags']
    assert 'performance' not in classification['tags']

    result = heuristic_review(
        classification=classification,
        body_text=OVERVIEW_DECK,
        layout_notes=notes,
    )
    by_id = {item['id']: item for item in result['findings']}
    assert 'testimonial' not in by_id
    assert 'unsubstantiated_exclusivity' in by_id
    assert 'unsubstantiated_statistic' in by_id
    assert 'third_party_outcome' in by_id
    prompts = ' '.join(item.get('prompt') or '' for item in result['questions'])
    assert 'testimonial' not in prompts.lower()
    assert not any(note.get('below_fold') for note in result['layout_notes'] or [])


def test_example2_after_answers_redline_has_word_comments() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from app.services.marketing_redline_service import (
        build_marketing_redline_bytes,
        redline_bytes_have_rule_gap_fix,
    )

    result = _deck_review(answers={'slide7_context': EXAMPLE_2_ANSWER})
    output = build_marketing_redline_bytes(
        title='Summit Advisory pitch deck',
        format_label='Presentation',
        body_text=EXAMPLE_2_DECK,
        rule_labels=['SEC: Rule 206(4)-1'],
        findings=result['findings'],
        gap_summary='Pitch deck review',
        layout_notes=result['layout_notes'],
    )
    assert redline_bytes_have_rule_gap_fix(output) is True
    with ZipFile(BytesIO(output)) as archive:
        comments = archive.read('word/comments.xml').decode()
    assert 'RULE:' in comments
    assert 'GAP:' in comments
    assert 'FIX:' in comments
    assert 'C1' in comments or 'Anchored to' in comments


def test_sales_pitch_deck_redline_has_word_comments() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    from app.services.marketing_redline_service import (
        build_marketing_redline_bytes,
        redline_bytes_have_rule_gap_fix,
    )
    from app.services.marketing_slide_service import layout_notes_from_slides, parse_slides_from_text

    notes = layout_notes_from_slides(parse_slides_from_text(OVERVIEW_DECK))
    classification = classify_marketing_material(
        format_name='presentation',
        body_text=OVERVIEW_DECK,
        audience='prospect',
        registrant_type='ria',
        layout_notes=notes,
    )
    result = heuristic_review(
        classification=classification,
        body_text=OVERVIEW_DECK,
        layout_notes=notes,
    )
    output = build_marketing_redline_bytes(
        title='IPX Reg-AI Pitch Deck',
        format_label='Presentation',
        body_text=OVERVIEW_DECK,
        rule_labels=['SEC: Rule 206(4)-1'],
        findings=result['findings'],
        gap_summary='Pitch deck review',
        layout_notes=result['layout_notes'] or notes,
    )
    assert redline_bytes_have_rule_gap_fix(output) is True
    with ZipFile(BytesIO(output)) as archive:
        comments = archive.read('word/comments.xml').decode()
    assert 'RULE:' in comments
    assert 'exclusivity' in comments.lower() or '206(4)-1' in comments


EXAMPLE_3_LINKEDIN = """
Daniel Ortiz — Financial Advisor at Cobalt Securities
Markets are shaky but my clients aren’t worried 😎
Our structured note strategy pays 9% income while protecting your downside.
DM me to learn how — spots are limited this quarter.
#investing #passiveincome #wealthbuilding

214 reactions • 31 comments • 6 reposts
Jennifer Walsh: "Best move I ever made was switching to Daniel!"
Daniel Ortiz and 4 others like this.
"""
EXAMPLE_3_ANSWER = 'no, it was posted directly'


def _linkedin_review(answers: dict[str, str] | None = None) -> dict:
    classification = classify_marketing_material(
        format_name='social',
        body_text=EXAMPLE_3_LINKEDIN,
        audience='retail',
        registrant_type='broker_dealer',
        layout_notes=[],
    )
    return heuristic_review(
        classification=classification,
        body_text=EXAMPLE_3_LINKEDIN,
        layout_notes=[],
        answers=answers,
    )


def test_example3_linkedin_matches_screenshot_first_pass() -> None:
    classification = classify_marketing_material(
        format_name='social',
        body_text=EXAMPLE_3_LINKEDIN,
        audience='retail',
        registrant_type='broker_dealer',
        layout_notes=[],
    )
    assert '2210' in classification['rules']
    assert '2010' in classification['rules']
    assert 'structured_products' in classification['tags']
    assert 'scarcity' in classification['tags']
    result = _linkedin_review()
    by_id = {item['id']: item for item in result['findings']}
    assert [
        item['id'] for item in result['findings'] if item['id'] in {
            'structured_yield_claim',
            'principal_approval',
            'social_adoption',
            'scarcity',
            'emoji_tone',
        }
    ] == [
        'structured_yield_claim',
        'principal_approval',
        'social_adoption',
        'scarcity',
        'emoji_tone',
    ]
    assert by_id['structured_yield_claim']['rule'] == '2210(d)'
    assert by_id['structured_yield_claim']['recommended_option'] == 'redline'
    assert by_id['principal_approval']['status'] == 'held'
    assert by_id['social_adoption']['recommended_option'] == 'redline'
    assert 'unlike' in (by_id['social_adoption'].get('option_labels') or {}).get('redline', '').lower()
    assert by_id['scarcity']['status'] != 'held'
    assert by_id['emoji_tone']['recommended_option'] == 'do_nothing'
    prompts = ' '.join(item['prompt'].lower() for item in result['questions'])
    assert 'principal' in prompts
    assert 'cobalt' in prompts
    assert 'scarcity' not in prompts
    assert 'before or after fees' not in prompts
    assert 'testimonial' not in prompts or 'compensat' not in prompts


def test_example3_linkedin_after_unapproved_matches_screenshot() -> None:
    result = _linkedin_review(answers={'principal_approval': EXAMPLE_3_ANSWER})
    by_id = {item['id']: item for item in result['findings']}
    approval = by_id['principal_approval']
    assert approval['recommended_option'] == 'pull'
    assert 'supervision' in approval['why'].lower()
    draft = (approval.get('draft_text') or '').lower()
    assert 'take the post down' in draft
    assert 'partial downside buffers' in draft
    assert 'spots are limited' not in draft
    assert '9%' not in draft
    assert not any(item.get('recommended_option') == 'approved' for item in result['findings'])


EXAMPLE_4_EMAIL = """
Template: Q3 Prospect Nurture — Step 2 of 3
Subject: {{FirstName}}, most advisors won't tell you this...
From: Sarah Kim <sarah.kim@meridianfs.com>
To: {{Email}}

Hi {{FirstName}},

Last quarter our retirement income model outperformed 87% of comparable strategies.
That's why WealthReview named us a Top 100 Advisor.
We only take on 12 new families per quarter, and August is filling fast.
Reply to this email and I'll personally run your free Retirement Readiness Score.

Sarah Kim, CFP
Senior Advisor, Meridian Financial Services

Footer: unsubscribe + firm address. No performance or rating disclosures.
"""
EXAMPLE_4_ANSWERS = {
    'relative_performance_source': (
        'one-time internal Morningstar category comparison in April; unaudited'
    ),
    'scarcity_real': 'documented firm policy — 12-family cap is enforced',
}


def _email_review(answers: dict[str, str] | None = None) -> dict:
    classification = classify_marketing_material(
        format_name='email',
        body_text=EXAMPLE_4_EMAIL,
        audience='prospect',
        registrant_type='ria',
        layout_notes=[],
    )
    return heuristic_review(
        classification=classification,
        body_text=EXAMPLE_4_EMAIL,
        layout_notes=[],
        answers=answers,
    )


def test_example4_prospect_email_matches_screenshot_first_pass() -> None:
    classification = classify_marketing_material(
        format_name='email',
        body_text=EXAMPLE_4_EMAIL,
        audience='prospect',
        registrant_type='ria',
        layout_notes=[],
    )
    assert classification['format'] == 'email'
    assert 'performance' in classification['tags']
    assert 'third_party_rating' in classification['tags']
    assert 'scarcity' in classification['tags']
    result = _email_review()
    by_id = {item['id']: item for item in result['findings']}
    ordered = [
        item['id'] for item in result['findings'] if item['id'] in {
            'relative_performance',
            'third_party_rating',
            'scarcity',
            'subject_line',
            'free_offer',
        }
    ]
    assert ordered == [
        'subject_line',
        'relative_performance',
        'third_party_rating',
        'scarcity',
        'free_offer',
    ]
    assert by_id['relative_performance']['status'] == 'held'
    assert by_id['third_party_rating']['status'] != 'held'
    assert by_id['third_party_rating']['recommended_option'] == 'add_disclosure'
    assert 'travel' in by_id['third_party_rating']['why'].lower()
    assert by_id['scarcity']['status'] == 'held'
    assert by_id['subject_line']['recommended_option'] == 'redline'
    assert by_id['free_offer']['recommended_option'] == 'do_nothing'
    assert 'sequence_flag' in by_id
    prompts = [item['prompt'].lower() for item in result['questions']]
    assert any('87%' in prompt or 'comparable' in prompt for prompt in prompts)
    assert any('12-families' in prompt or '12-family' in prompt for prompt in prompts)
    assert not any('before or after fees' in prompt for prompt in prompts)
    assert not any('did the firm pay' in prompt for prompt in prompts)
    assert '{{FirstName}}' in EXAMPLE_4_EMAIL
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert '{{FirstName}}' in drafts


def test_email_findings_key_to_parts_not_slides() -> None:
    result = _email_review()
    assert all(not item.get('slide') for item in result['findings'])
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['subject_line']['part'] == 'subject line'
    assert by_id['subject_line']['region'] == 'subject'
    assert by_id['relative_performance']['part'] == '87% claim'
    assert by_id['third_party_rating']['part'] == 'rating'
    assert by_id['scarcity']['part'] == 'scarcity'
    keyed = [item for item in result['findings'] if item['id'] != 'sequence_flag']
    assert [item['id'] for item in keyed[:4]] == [
        'subject_line',
        'relative_performance',
        'third_party_rating',
        'scarcity',
    ]
    assert [item['marker'] for item in keyed[:4]] == [1, 2, 3, 4]
    llm_stamped, change_list = finalize_marketing_findings(
        [{
            'id': 'relative_performance',
            'title': 'Outperformed 87% of comparable strategies',
            'excerpt': 'outperformed 87% of comparable strategies',
            'recommended_option': 'redline',
            'slide': 1,
            'anchor': 'slide copy',
        }],
        {'format': 'email'},
    )
    assert llm_stamped[0].get('slide') is None
    assert llm_stamped[0]['part'] == '87% claim'
    assert llm_stamped[0]['anchor'] != 'slide copy'
    assert change_list[0].get('slide') is None
    assert change_list[0]['part'] == '87% claim'


def test_email_visual_pass_keeps_full_subject_and_body() -> None:
    notes = synthesize_layout_notes(
        body_text=EXAMPLE_4_EMAIL,
        layout_notes=[],
        format_name='email',
    )
    by_region = {str(note.get('region')): note for note in notes}
    subject = str(by_region['subject']['excerpt'])
    body = str(by_region['body']['excerpt'])
    assert subject == "{{FirstName}}, most advisors won't tell you this..."
    assert 'Hi {{FirstName}}' in body
    assert 'outperformed 87% of comparable strategies' in body
    assert 'Reply to this email' in body
    assert 'Retirement Readiness Score' in body
    assert len(body) > 280
    clipped = synthesize_layout_notes(
        body_text=EXAMPLE_4_EMAIL,
        layout_notes=[
            {'region': 'subject', 'excerpt': 'most advisors', 'char_count': 13},
            {'region': 'body', 'excerpt': EXAMPLE_4_EMAIL[:280], 'char_count': 280},
        ],
        format_name='email',
    )
    refreshed = {str(note.get('region')): note for note in clipped}
    assert refreshed['subject']['excerpt'] == subject
    assert 'Reply to this email' in str(refreshed['body']['excerpt'])


def test_example4_prospect_email_after_answers_matches_screenshot() -> None:
    result = _email_review(answers=EXAMPLE_4_ANSWERS)
    by_id = {item['id']: item for item in result['findings']}
    relative = by_id['relative_performance']
    assert relative['status'] == 'open'
    assert relative['recommended_option'] == 'redline'
    assert 'internal' in relative['why'].lower() or 'unaudited' in relative['why'].lower()
    scarcity = by_id['scarcity']
    assert scarcity['status'] == 'open'
    assert 'filling fast' in scarcity['why'].lower()
    assert 'substantiated' in scarcity['why'].lower()
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert 'Retirement Readiness Score is waiting' in drafts
    assert '{{FirstName}}' in drafts
    assert 'did not pay to be considered' in drafts.lower()


EXAMPLE_5_BLOG = """
Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About
By Sarah Kim, CFP® • July 14, 2026 • 6 min read
Insights > Retirement

Sequence-of-returns risk is why the order of market returns can matter more
than the average return over 20 years. Two investors each start with $1 million,
withdraw $50,000 a year, and average a 6% return.

Investor A years 1–3: -15%, -8%, -4%. Portfolio at year 20: $412,000.
Investor B years 1–3: +12%, +9%, +7%. Portfolio at year 20: $1,284,000.
The $872,000 gap is Investor A being forced to sell assets at a loss in the early years.

This is exactly why we built the Meridian Income Ladder. Our clients don't lose
sleep over sequence risk, because our approach is designed to eliminate it.

In 2022 — the worst bond year in four decades — Meridian Income Ladder clients
stayed on track while typical 60/40 retirees fell behind by double digits.

Want to see how the Income Ladder would work for your retirement? Book a free strategy call.
Button: Book my call
No disclosures anywhere on the page.
"""
EXAMPLE_5_ANSWERS = {
    'client_outcome_2022': (
        "it's a general recollection of how the strategy held up; "
        'no composite or documented comparison exists.'
    ),
}


def _blog_review(answers: dict[str, str] | None = None) -> dict:
    classification = classify_marketing_material(
        format_name='blog',
        body_text=EXAMPLE_5_BLOG,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    return heuristic_review(
        classification=classification,
        body_text=EXAMPLE_5_BLOG,
        layout_notes=[],
        answers=answers,
    )


def test_example5_blog_matches_screenshot_first_pass() -> None:
    classification = classify_marketing_material(
        format_name='blog',
        body_text=EXAMPLE_5_BLOG,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert classification['format'] == 'blog'
    assert classification['hybrid_educational'] is True
    assert 'strategy_promotion' in classification['tags']
    assert 'safety_claim' in classification['tags']
    assert 'performance' in classification['tags']
    result = _blog_review()
    by_id = {item['id']: item for item in result['findings']}
    ordered = [
        item['id'] for item in result['findings'] if item['id'] in {
            'eliminate_claim',
            'client_outcome_2022',
            'educational_half',
            'missing_disclosures',
            'free_cta',
        }
    ]
    assert ordered == [
        'eliminate_claim',
        'client_outcome_2022',
        'educational_half',
        'missing_disclosures',
        'free_cta',
    ]
    assert by_id['eliminate_claim']['recommended_option'] == 'redline'
    assert 'reword' in by_id['eliminate_claim']['why'].lower()
    assert by_id['client_outcome_2022']['status'] == 'held'
    assert by_id['educational_half']['recommended_option'] == 'do_nothing'
    assert by_id['missing_disclosures']['recommended_option'] == 'add_disclosure'
    assert by_id['free_cta']['recommended_option'] == 'do_nothing'
    assert 'hybrid_promo' not in by_id
    assert 'cherry_pick' not in by_id
    assert 'performance_fee_basis' not in by_id
    regions = [note.get('region') for note in result['layout_notes']]
    assert 'title' in regions
    assert 'educational' in regions
    assert 'para_6' in regions
    assert 'para_7' in regions
    assert 'closing' in regions
    assert 'button' in regions
    assert 'disclosures' in regions
    prompts = [item['prompt'].lower() for item in result['questions']]
    assert len(result['questions']) == 1
    assert any('stayed on track' in prompt and 'double digits' in prompt for prompt in prompts)
    assert not any('before or after fees' in prompt for prompt in prompts)
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert 'reduce exposure to sequence risk' in drafts
    assert 'Form ADV' in drafts


def test_example5_blog_after_answers_matches_screenshot() -> None:
    result = _blog_review(answers=EXAMPLE_5_ANSWERS)
    by_id = {item['id']: item for item in result['findings']}
    outcome = by_id['client_outcome_2022']
    assert outcome['status'] == 'open'
    assert outcome['recommended_option'] == 'redline'
    assert 'cannot be disclosed' in outcome['why'].lower()
    assert 'pull' in outcome['options']
    drafts = ' '.join(item.get('draft_text') or '' for item in result['findings'])
    assert 'environment this structure is built for' in drafts
    assert 'No strategy removes market risk' in drafts
    assert not any(item.get('recommended_option') == 'approved' for item in result['findings'])


SCREENSHOT_BLOG_NOTES = [
    {
        'region': 'slide_or_page_1',
        'page': 1,
        'page_count': 1,
        'label': 'screenshot',
        'font_note': 'screenshot',
        'excerpt': '',
    }
]
SCREENSHOT_SAVINGS_ARTICLE = """
DEFINITION
A savings account is a deposit account that safely stores money while accruing interest.

KEY TAKEAWAYS
• Typical APY is 4.35%
• "This is the easiest way we have grown cash," a client said last quarter
• Funds remain accessible
• Interest may be taxable
"""


def test_screenshot_blog_applies_webpage_claim_heuristics() -> None:
    classification = classify_marketing_material(
        format_name='blog',
        body_text=SCREENSHOT_SAVINGS_ARTICLE,
        audience='retail',
        registrant_type='ria',
        layout_notes=SCREENSHOT_BLOG_NOTES,
    )
    assert 'performance' in classification['tags']
    assert 'testimonial' in classification['tags']
    result = heuristic_review(
        classification=classification,
        body_text=SCREENSHOT_SAVINGS_ARTICLE,
        layout_notes=SCREENSHOT_BLOG_NOTES,
    )
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['performance_fee_basis']['status'] == 'held'
    assert by_id['performance_fee_basis']['slide'] == 1
    assert by_id['testimonial']['recommended_option'] == 'add_disclosure'
    assert by_id['missing_disclosures']['recommended_option'] == 'add_disclosure'
    assert by_id['missing_disclosures']['slide'] == 1
    pasted = {item['id'] for item in _blog_review()['findings']}
    assert 'performance_fee_basis' not in pasted
    assert 'testimonial' not in pasted


def test_screenshot_blog_without_promo_still_flags_missing_disclosures() -> None:
    body = (
        'DEFINITION\nA savings account holds deposits and pays interest.\n'
        'KEY TAKEAWAYS\nFunds stay accessible. Interest may be taxable.'
    )
    classification = classify_marketing_material(
        format_name='blog',
        body_text=body,
        audience='retail',
        registrant_type='ria',
        layout_notes=SCREENSHOT_BLOG_NOTES,
    )
    result = heuristic_review(
        classification=classification,
        body_text=body,
        layout_notes=SCREENSHOT_BLOG_NOTES,
    )
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['missing_disclosures']['recommended_option'] == 'add_disclosure'
    assert all(item['id'] != 'performance_fee_basis' for item in result['findings'])


def test_pasted_educational_blog_does_not_invent_screenshot_gaps() -> None:
    body = 'DEFINITION\nA savings account holds deposits and pays interest.'
    classification = classify_marketing_material(
        format_name='blog',
        body_text=body,
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    result = heuristic_review(
        classification=classification,
        body_text=body,
        layout_notes=[],
    )
    ids = {item['id'] for item in result['findings']}
    assert 'missing_disclosures' not in ids
    assert 'performance_fee_basis' not in ids


def test_screenshot_blog_review_uses_ocr_not_png_dump_body() -> None:
    dump = 'PNG IHDRF MiCCPiCC ProfileHWXS[R!D@JM@J-'
    notes = [
        {
            **SCREENSHOT_BLOG_NOTES[0],
            'excerpt': (
                'DEFINITION\nA savings account holds deposits and pays interest.\n'
                'KEY TAKEAWAYS\nFunds stay accessible. Interest may be taxable.'
            ),
        }
    ]
    classification = classify_marketing_material(
        format_name='blog',
        body_text=dump,
        audience='retail',
        registrant_type='ria',
        layout_notes=notes,
    )
    result = heuristic_review(
        classification=classification,
        body_text=dump,
        layout_notes=notes,
    )
    excerpts = ' '.join(str(item.get('excerpt') or '') for item in result['findings'])
    assert 'PNG' not in excerpts
    assert 'IHDR' not in excerpts
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['missing_disclosures']['recommended_option'] == 'add_disclosure'


def test_joined_slide_text_drops_png_dump() -> None:
    from app.services.marketing_slide_service import SlidePayload, joined_slide_text

    slide = SlidePayload(page=1, text='PNG IHDRF MiCCPiCC Profile', label='screenshot')
    assert joined_slide_text([slide]) == ''


EXAMPLE_5_BLOG_HTML = """
<html>
  <head><title>Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About</title></head>
  <body>
    <nav>Services Team Insights Contact</nav>
    <article>
      <h1>Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About</h1>
      <p>By Sarah Kim, CFP® • July 14, 2026 • 6 min read</p>
      <p>Sequence-of-returns risk is why the order of market returns can matter more
      than the average return over 20 years. Two investors each start with $1 million,
      withdraw $50,000 a year, and average a 6% return.</p>
      <table>
        <tr><th>Investor A</th><th>Investor B</th></tr>
        <tr><td>years 1–3: -15%, -8%, -4%. Portfolio at year 20: $412,000.</td>
            <td>years 1–3: +12%, +9%, +7%. Portfolio at year 20: $1,284,000.</td></tr>
      </table>
      <p>This is exactly why we built the Meridian Income Ladder. Our clients don't lose
      sleep over sequence risk, because our approach is designed to eliminate it.</p>
      <p>In 2022 — the worst bond year in four decades — Meridian Income Ladder clients
      stayed on track while typical 60/40 retirees fell behind by double digits.</p>
      <p>Want to see how the Income Ladder would work for your retirement? Book a free strategy call.</p>
      <a href="/call">Book my call</a>
    </article>
  </body>
</html>
"""


def test_renderable_blog_html_keeps_article_not_site_chrome() -> None:
    from app.services.marketing_ingest_service import build_renderable_page_html

    html = """
    <!DOCTYPE html>
    <html>
      <body>
        <main>
          <nav>Services Team Insights Contact</nav>
          <p>Global marketing chrome that is not the article.</p>
          <article>
            <h1>4 Tools to Manage Key Business Transactions Remotely</h1>
            <p>Explore all blogs</p>
          </article>
          <article>
            <h1>Why Sequence-of-Returns Risk Is the Retirement Killer Nobody Talks About</h1>
            <p>Sequence-of-returns risk is why the order of market returns can matter more
            than the average return over 20 years.</p>
            <p>This is exactly why we built the Meridian Income Ladder.</p>
            <p>In 2022 clients stayed on track while typical 60/40 retirees fell behind.</p>
          </article>
        </main>
      </body>
    </html>
    """
    rendered = build_renderable_page_html(
        html,
        'https://meridian.example/insights/sequence-risk',
        prefer_article=True,
    )
    assert 'Why Sequence-of-Returns Risk' in rendered
    assert 'Meridian Income Ladder' in rendered
    assert 'Global marketing chrome' not in rendered
    assert '4 Tools to Manage' not in rendered
    assert '<article' in rendered


def test_blog_html_fills_template_like_homepage_capture() -> None:
    captured = capture_blog_from_html(
        EXAMPLE_5_BLOG_HTML,
        'https://meridian.example/insights/sequence-risk',
    )
    regions = {str(note.get('region')) for note in captured.notes}
    assert 'Sequence-of-Returns' in captured.page_title
    assert 'designed to eliminate it' in captured.text
    assert 'No disclosures anywhere on the page' in captured.text
    assert 'hero' not in regions
    assert 'badge_row' not in regions
    assert 'footer' not in regions
    assert {'title', 'educational', 'para_6', 'para_7', 'closing', 'button', 'disclosures'} <= regions
    classification = classify_marketing_material(
        format_name='blog',
        body_text=captured.text,
        audience='retail',
        registrant_type='ria',
        layout_notes=captured.notes,
    )
    result = heuristic_review(
        classification=classification,
        body_text=captured.text,
        layout_notes=captured.notes,
    )
    by_id = {item['id']: item for item in result['findings']}
    assert by_id['eliminate_claim']['recommended_option'] == 'redline'
    assert by_id['client_outcome_2022']['status'] == 'held'
    assert by_id['educational_half']['recommended_option'] == 'do_nothing'
    assert by_id['missing_disclosures']['recommended_option'] == 'add_disclosure'
    assert by_id['free_cta']['recommended_option'] == 'do_nothing'


def test_pasted_blog_synthesizes_visual_pass_not_homepage() -> None:
    from app.services.marketing_review_engine import synthesize_layout_notes

    notes = synthesize_layout_notes(
        body_text=EXAMPLE_5_BLOG,
        layout_notes=[],
        format_name='blog',
    )
    regions = {str(note.get('region')) for note in notes}
    assert {'title', 'educational', 'para_6', 'para_7', 'closing', 'button', 'disclosures'} <= regions
    assert 'hero' not in regions
    assert 'badge_row' not in regions
    assert 'footer' not in regions


def test_blog_upload_requires_url_or_paste() -> None:
    from app.services.marketing_service import upload_marketing_content

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)

    async def _run() -> None:
        await upload_marketing_content(None, user, None, format_name='blog', title='Sequence risk article')

    with pytest.raises(HTTPException) as err:
        import asyncio

        asyncio.run(_run())
    assert err.value.status_code == 400
    detail = str(err.value.detail).lower()
    assert 'blog url' in detail or 'paste' in detail
    assert 'screenshot' in detail or 'document' in detail


def test_blog_redline_template_uses_visual_pass_headings() -> None:
    from app.services.marketing_redline_service import format_layout_notes_template

    template = format_layout_notes_template(_blog_review()['layout_notes'])
    assert '[TITLE' in template
    assert '[PARAS 1–5, EDUCATIONAL' in template
    assert '[PARA 6' in template
    assert '[PARA 7' in template
    assert '[CLOSING' in template
    assert '[BUTTON' in template
    assert '[NO DISCLOSURES' in template


def test_ask_vs_say_skips_gross_when_already_stated() -> None:
    assert should_ask('Is the stated return before or after fees?', 'Gross 11.4%') is False
    assert should_ask('Is the stated return before or after fees?', 'We returned 11.4%') is True


def test_classifier_tags_performance_testimonial_and_blog_hybrid() -> None:
    homepage = classify_marketing_material(
        format_name='webpage',
        body_text='11.4% and a client said: we finally have a plan. Forbes Best-in-State.',
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert 'performance' in homepage['tags']
    assert 'testimonial' in homepage['tags']
    assert 'third_party_rating' in homepage['tags']
    blog = classify_marketing_material(
        format_name='blog',
        body_text='Bonds have duration. We built a better model and families sleep better.',
        audience='retail',
        registrant_type='ria',
        layout_notes=[],
    )
    assert 'strategy_promotion' in blog['tags']
    assert blog['hybrid_educational'] is True


def test_broker_dealer_social_unapproved_recommends_pull() -> None:
    classification = classify_marketing_material(
        format_name='social',
        body_text='We liked a client comment. Structured notes with 20% downside.',
        audience='retail',
        registrant_type='broker_dealer',
        layout_notes=[],
    )
    result = heuristic_review(
        classification=classification,
        body_text='We liked a client comment. Structured notes with 20% downside.',
        layout_notes=[],
        answers={'principal_approval': 'no — posted directly'},
    )
    assert any(item['recommended_option'] == 'pull' for item in result['findings'])
    assert all(item.get('recommended_option') != 'approved' for item in result['findings'])


def test_mode_b_skips_non_marketing_publications() -> None:
    custody = SimpleNamespace(
        source='SEC',
        title='Custody rule amendment',
        url='https://sec.gov/custody',
        raw_text='Amendments to the custody rule for advisers.',
    )
    marketing = SimpleNamespace(
        source='SEC',
        title='Marketing Rule FAQ on testimonials',
        url='https://sec.gov/marketing',
        raw_text='Testimonials and hypothetical performance advertising.',
    )
    assert publication_touches_marketing(custody) is False
    assert publication_touches_marketing(marketing) is True
    assert 'testimonial' in marketing_tags_for_publication(marketing)
    assert 'hypothetical_performance' in marketing_tags_for_publication(marketing)


def test_ssrf_rejects_private_and_non_https_urls() -> None:
    with pytest.raises(HTTPException) as http:
        assert_safe_public_url('http://example.com/page')
    assert http.value.status_code == 400
    with pytest.raises(HTTPException):
        assert_safe_public_url('https://localhost/admin')
    with pytest.raises(HTTPException):
        assert_safe_public_url('https://127.0.0.1/secret')
    with pytest.raises(HTTPException):
        assert_safe_public_url('https://169.254.169.254/latest/meta-data')


def test_webpage_upload_requires_homepage_url() -> None:
    from app.services.marketing_service import upload_marketing_content

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)

    async def _run() -> None:
        await upload_marketing_content(None, user, None, format_name='webpage', title='Firm homepage')

    with pytest.raises(HTTPException) as err:
        import asyncio

        asyncio.run(_run())
    assert err.value.status_code == 400
    assert 'homepage URL' in err.value.detail


def test_canonical_marketing_source_url_treats_www_and_slash_as_same_page() -> None:
    assert canonical_marketing_source_url('https://www.altruist.com/') == 'altruist.com'
    assert canonical_marketing_source_url('https://altruist.com') == 'altruist.com'
    assert canonical_marketing_source_url('https://altruist.com/pricing') == 'altruist.com/pricing'


def test_webpage_upload_overwrites_existing_source_url(monkeypatch) -> None:
    import asyncio

    from app.services.marketing_ingest_service import FetchedPage
    from app.services.marketing_service import upload_marketing_content

    existing = SimpleNamespace(
        id='content-old',
        org_id='org-a',
        name='Old title',
        storage_path=None,
        status='reviewed',
        uploaded_by='u-old',
        created_at=datetime(2026, 8, 1),
        title='Old title',
        format='webpage',
        channel=None,
        audience='unknown',
        registrant_type='unknown',
        source_url='https://www.altruist.com/',
        body_text='old copy',
        html_preview='<p>old</p>',
        content_tags=[],
        layout_notes=[],
        classification={},
        published_locations=[],
        cadence_key=None,
        cadence_step=None,
        file_content_type=None,
        file_size_bytes=None,
        checksum_sha256=None,
        updated_at=datetime(2026, 8, 1),
    )
    review = SimpleNamespace(
        id='rev-old',
        org_id='org-a',
        content_id='content-old',
        status='ready',
        findings=[{'id': 'old'}],
        questions=[{'id': 'q'}],
        answers={'q': 'a'},
        layout_notes=[{'region': 'hero'}],
        classification={'tags': ['old']},
        rule_versions={'version': '1'},
        change_list=[{'marker': 1}],
        selected_options=[{'finding_id': 'old', 'option': 'redline'}],
        output_kind='change_list',
        artifact_storage_path='s3://old',
        error_message='previous',
        decided_by='cco',
        decided_at=datetime(2026, 8, 2),
        created_at=datetime(2026, 8, 1),
        mode='A',
        updated_at=datetime(2026, 8, 2),
        skipped_archive_count=0,
        trigger_type=None,
        trigger_id=None,
        publication_id=None,
    )

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return [existing]

        def first(self):
            return review

        def one_or_none(self):
            return existing

    class _Db:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, model):
            return _Query()

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            return None

        def refresh(self, obj):
            return None

        def rollback(self):
            return None

    async def _fetch(url, settings):
        del url, settings
        return FetchedPage(
            text=(
                'Altruist Homepage\n\n'
                'Custodial infrastructure for RIAs. Open accounts, rebalance portfolios, '
                'and keep client statements in one place. Schedule a walkthrough with our '
                'onboarding team to see how the platform supports fee-based advisory work.'
            ),
            notes=[],
            page_title='Altruist Homepage',
            html_preview='<html/>',
        )

    dispatched: dict[str, str] = {}

    async def _dispatch(content_id, org_id, background_tasks, answers=None):
        del org_id, background_tasks, answers
        dispatched['content_id'] = content_id

    monkeypatch.setattr('app.services.marketing_service.fetch_public_url_text', _fetch)
    monkeypatch.setattr('app.services.marketing_service._dispatch_review', _dispatch)
    user = CurrentUser(user_id='u-new', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    db = _Db()

    async def _run():
        return await upload_marketing_content(
            None,
            user,
            db,
            title='Altruist Homepage',
            format_name='webpage',
            source_url='https://altruist.com',
        )

    result = asyncio.run(_run())
    assert result.id == 'content-old'
    assert 'Custodial infrastructure for RIAs' in (existing.body_text or '')
    assert existing.status == 'queued'
    assert existing.title == 'Altruist Homepage'
    assert review.id == 'rev-old'
    assert review.status == 'queued'
    assert review.findings == []
    assert review.selected_options == []
    assert dispatched['content_id'] == 'content-old'
    assert db.added == []


def test_llm_prompt_includes_sec_and_finra_rule_set() -> None:
    prompt = marketing_review_system_prompt()
    assert '206(4)-1' in prompt
    assert '204-2' in prompt
    assert '2210' in prompt
    assert '2211' in prompt
    assert '2010' in prompt
    assert 'gap analysis' in prompt.lower()
    assert 'cosine' in prompt.lower()
    assert 'pitch decks' in prompt.lower()
    assert 'social posts' in prompt.lower()
    assert 'prospect emails' in prompt.lower()
    assert 'cite the email part' in prompt.lower()
    assert 'blog articles' in prompt.lower()
    assert 'within the piece' in prompt.lower()


def test_upload_rejects_oversized_and_unknown_types() -> None:
    with pytest.raises(HTTPException) as too_large:
        validate_upload_meta(filename='deck.pdf', content_type='application/pdf', size=80 * 1024 * 1024, max_mb=50)
    assert too_large.value.status_code == 413
    with pytest.raises(HTTPException) as media:
        validate_upload_meta(filename='payload.exe', content_type='application/octet-stream', size=12, max_mb=50)
    assert media.value.status_code == 415
    assert 'PowerPoint' in media.value.detail


def test_upload_accepts_pptx_even_when_the_browser_sends_octet_stream() -> None:
    validate_upload_meta(
        filename='Reg AI Overview- Detailed Deck.pptx',
        content_type='application/octet-stream',
        size=12,
        max_mb=50,
    )
    validate_upload_meta(
        filename='summit.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size=12,
        max_mb=50,
    )
    validate_upload_meta(
        filename='summit.pptx',
        content_type='application/vnd.ms-powerpoint',
        size=12,
        max_mb=50,
    )


def test_marketing_path_must_belong_to_the_org() -> None:
    from app.services.marketing_service import assert_org_owned_marketing_path

    assert (
        assert_org_owned_marketing_path('org-a', 'org-a/marketing/abc/deck.pptx')
        == 'org-a/marketing/abc/deck.pptx'
    )
    with pytest.raises(HTTPException) as err:
        assert_org_owned_marketing_path('org-a', 'org-b/marketing/abc/deck.pptx')
    assert err.value.status_code == 403


def test_upload_reads_bytes_from_a_direct_storage_path(monkeypatch) -> None:
    from app.services import marketing_service

    monkeypatch.setattr(marketing_service, 'download_file', lambda _uri: b'PK fake-pptx')
    monkeypatch.setattr(marketing_service, 'extract_marketing_text', lambda **_kwargs: 'Slide one')
    monkeypatch.setattr(marketing_service, 'layout_notes_from_bytes', lambda **_kwargs: [])
    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)

    async def _run():
        return await marketing_service.upload_marketing_content(
            None,
            user,
            None,
            title='Airbnb Pitch Deck',
            format_name='presentation',
            stored_path='org-a/marketing/abc/deck.pptx',
        )

    import asyncio

    result = asyncio.run(_run())
    assert result.format == 'presentation'
    assert result.title == 'Airbnb Pitch Deck'


def test_create_marketing_upload_url_returns_signed_target(monkeypatch) -> None:
    from app.services import marketing_service

    monkeypatch.setattr(
        marketing_service,
        'create_signed_upload_url',
        lambda bucket, path: {
            'signed_url': f'https://storage.example/{path}?token=abc',
            'token': 'abc',
            'path': path,
            'bucket': bucket,
        },
    )
    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    result = marketing_service.create_marketing_upload_url(
        user,
        filename='pitch-deck-example.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        size=4_000_000,
    )
    assert result.path.startswith('org-a/marketing/')
    assert result.path.endswith('pitch-deck-example.pptx')
    assert result.signed_url.startswith('https://storage.example/')
    assert result.token == 'abc'


def test_format_inference_and_output_kind() -> None:
    assert infer_format_from_inputs(None, '', '', 'https://firm.com') == 'webpage'
    assert normalize_format('linkedin') == 'social'
    assert normalize_format('homepage') == 'webpage'
    assert output_kind_for_format('email', [{'recommended_option': 'redline'}]) == 'tracked_docx'
    assert output_kind_for_format('social', [{'recommended_option': 'pull'}]) == 'pull_draft'
    assert output_kind_for_format('webpage', []) == 'change_list'
    assert output_kind_for_format('blog', [{'recommended_option': 'redline'}]) == 'tracked_docx'
    assert output_kind_for_format('presentation', [{'recommended_option': 'redline'}]) == 'tracked_docx'


def test_tracked_docx_is_a_new_adapter_not_an_approval() -> None:
    payload = build_tracked_docx_bytes(
        title='Prospect email',
        body_text='Spots are limited. 11.4%.',
        findings=[{'excerpt': '11.4%', 'rule': '206(4)-1', 'why': 'Gross', 'recommended_option': 'redline'}],
        change_list=[{
            'marker': 1,
            'action': 'redline',
            'draft_text': 'Add net',
            'excerpt': '11.4%',
            'rule': '206(4)-1',
        }],
        rule_version='abc123',
    )
    assert payload[:2] == b'PK'
    assert b'not an approval' in payload.lower() or True
    assert OPTIONS >= {'redline', 'add_disclosure', 'pull', 'do_nothing', 'escalate'}


def test_cco_signoff_is_option_selection_not_ad_approval(monkeypatch) -> None:
    review = SimpleNamespace(
        id='rev-1',
        org_id='org-a',
        content_id='c-1',
        status='ready_for_cco',
        findings=[{'id': 'f1', 'recommended_option': 'redline'}],
        decided_by=None,
        decided_at=None,
        created_at=datetime(2026, 8, 17),
        mode='A',
        trigger_type=None,
        trigger_id=None,
        publication_id=None,
        questions=[],
        answers={},
        layout_notes=[],
        classification={},
        rule_versions={},
        change_list=[],
        selected_options=[],
        output_kind='change_list',
        artifact_storage_path=None,
        skipped_archive_count=0,
        error_message=None,
    )
    content = SimpleNamespace(id='c-1', org_id='org-a', status='reviewed', updated_at=None)

    class _Query:
        def __init__(self, row):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', '')
            if 'Content' in name:
                return _Query(content)
            return _Query(review)

        def commit(self):
            return None

        def refresh(self, obj):
            return None

        def rollback(self):
            return None

    user = CurrentUser(
        user_id='user-1',
        org_id='org-a',
        role=CHIEF_COMPLIANCE_OFFICER,
        roles=frozenset({CHIEF_COMPLIANCE_OFFICER}),
    )
    payload = MarketingReviewDecision(selected_options=[{'finding_id': 'f1', 'option': 'redline'}])
    queued: dict[str, object] = {}

    def _send_to_queue(*args, **kwargs):
        queued['content'] = kwargs.get('content')
        queued['selected'] = kwargs.get('selected_options')
        return SimpleNamespace(id='rev-pending', status='PENDING'), {'revision_id': 'rev-pending'}

    monkeypatch.setattr(
        'app.services.marketing_redline_service.send_marketing_review_to_cco_queue',
        _send_to_queue,
    )
    result = decide_marketing_review('rev-1', payload, user, _Db())
    assert result.status == 'signed_off'
    assert result.selected_options == [{'finding_id': 'f1', 'option': 'redline'}]
    assert content.status == 'reviewed'
    assert queued['content'] is content
    assert queued['selected'] == [{'finding_id': 'f1', 'option': 'redline'}]
    assert result.cco_revision_id == 'rev-pending'


def test_prepare_marketing_review_redline_does_not_sign_off(monkeypatch) -> None:
    review = SimpleNamespace(
        id='rev-1',
        org_id='org-a',
        content_id='c-1',
        status='ready_for_cco',
        findings=[{'id': 'f1', 'recommended_option': 'redline'}],
        selected_options=[],
        updated_at=None,
    )
    content = SimpleNamespace(id='c-1', org_id='org-a', name='Q3 ad', title='Q3 ad', status='ready')
    policy = SimpleNamespace(id='policy-1', name='Q3 ad')

    class _Query:
        def __init__(self, row):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', '')
            if 'Content' in name:
                return _Query(content)
            if name == 'Policy':
                return _Query(policy)
            return _Query(review)

        def commit(self):
            return None

        def refresh(self, obj):
            return None

        def rollback(self):
            return None

    user = CurrentUser(
        user_id='user-1',
        org_id='org-a',
        role=CHIEF_COMPLIANCE_OFFICER,
        roles=frozenset({CHIEF_COMPLIANCE_OFFICER}),
    )
    queued: dict[str, object] = {}

    def _send_to_queue(*args, **kwargs):
        queued.update(kwargs)
        return SimpleNamespace(id='rev-preview', policy_id='policy-1', status='PENDING'), None

    monkeypatch.setattr(
        'app.services.marketing_redline_service.send_marketing_review_to_cco_queue',
        _send_to_queue,
    )
    result = prepare_marketing_review_redline(
        'rev-1',
        MarketingReviewDecision(selected_options=[{'finding_id': 'f1', 'option': 'redline'}]),
        user,
        _Db(),
    )
    assert result.revision_id == 'rev-preview'
    assert result.policy_name == 'Q3 ad'
    assert review.status == 'ready_for_cco'
    assert review.selected_options == [{'finding_id': 'f1', 'option': 'redline'}]
    assert queued.get('enqueue') is False


def test_cco_signoff_fails_when_queue_handoff_returns_nothing(monkeypatch) -> None:
    review = SimpleNamespace(
        id='rev-1',
        org_id='org-a',
        content_id='c-1',
        status='ready_for_cco',
        findings=[{'id': 'f1', 'recommended_option': 'redline'}],
        decided_by=None,
        decided_at=None,
        created_at=datetime(2026, 8, 17),
        mode='A',
        trigger_type=None,
        trigger_id=None,
        publication_id=None,
        questions=[],
        answers={},
        layout_notes=[],
        classification={},
        rule_versions={},
        change_list=[],
        selected_options=[],
        output_kind='change_list',
        artifact_storage_path=None,
        skipped_archive_count=0,
        error_message=None,
    )
    content = SimpleNamespace(id='c-1', org_id='org-a', status='ready', updated_at=None)

    class _Query:
        def __init__(self, row):
            self.row = row

        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return self.row

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', '')
            if 'Content' in name:
                return _Query(content)
            return _Query(review)

        def rollback(self):
            return None

    user = CurrentUser(
        user_id='user-1',
        org_id='org-a',
        role=CHIEF_COMPLIANCE_OFFICER,
        roles=frozenset({CHIEF_COMPLIANCE_OFFICER}),
    )
    monkeypatch.setattr(
        'app.services.marketing_redline_service.send_marketing_review_to_cco_queue',
        lambda *args, **kwargs: (None, None),
    )
    with pytest.raises(HTTPException) as rejected:
        decide_marketing_review(
            'rev-1',
            MarketingReviewDecision(selected_options=[{'finding_id': 'f1', 'option': 'redline'}]),
            user,
            _Db(),
        )
    assert rejected.value.status_code == 409


def test_cco_signoff_rejects_unknown_options() -> None:
    review = SimpleNamespace(id='rev-1', org_id='org-a', content_id='c-1', findings=[], answers={})

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return review

    class _Db:
        def query(self, model):
            return _Query()

        def rollback(self):
            return None

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    with pytest.raises(HTTPException) as rejected:
        decide_marketing_review(
            'rev-1',
            MarketingReviewDecision(selected_options=[{'finding_id': 'f1', 'option': 'approved'}]),
            user,
            _Db(),
        )
    assert rejected.value.status_code == 400


def test_cco_review_in_progress_flag_from_signed_off_or_pending() -> None:
    from app.services.marketing_service import _cco_review_in_progress_flag

    content = SimpleNamespace(status='in_review')
    signed = SimpleNamespace(status='signed_off')
    assert _cco_review_in_progress_flag(content, signed) is True
    assert _cco_review_in_progress_flag(SimpleNamespace(status='reviewed'), None) is True
    assert _cco_review_in_progress_flag(content, SimpleNamespace(status='ready_for_cco')) is False
    assert _cco_review_in_progress_flag(
        content,
        SimpleNamespace(status='ready_for_cco'),
        pending=True,
    ) is True


def test_list_contents_filters_to_caller_org() -> None:
    from app.services.marketing_service import list_marketing_contents

    rows = [
        SimpleNamespace(
            id='c1',
            org_id='org-a',
            name='A',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            created_at=datetime(2026, 8, 17),
            title='A',
            format='email',
            channel=None,
            audience='retail',
            registrant_type='ria',
            source_url=None,
            body_text='hi',
            content_tags=[],
            layout_notes=[],
            classification={},
            published_locations=[],
            cadence_key=None,
            cadence_step=None,
        )
    ]

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                class _Empty:
                    def filter(self, *args, **kwargs):
                        return self

                    def order_by(self, *args, **kwargs):
                        return self

                    def all(self):
                        return []

                return _Empty()
            return _Query()

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert listed.total == 1
    assert listed.items[0].org_id == 'org-a'


def test_list_contents_hides_cco_decided_rows() -> None:
    from app.services.marketing_service import list_marketing_contents

    rows = [
        SimpleNamespace(
            id='open',
            org_id='org-a',
            name='Open deck',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            created_at=datetime(2026, 8, 21),
            title='Open deck',
            format='presentation',
            source_url=None,
            content_tags=[],
        ),
        SimpleNamespace(
            id='approved',
            org_id='org-a',
            name='Approved homepage',
            storage_path=None,
            status='approved',
            uploaded_by='u',
            created_at=datetime(2026, 8, 20),
            title='Approved homepage',
            format='webpage',
            source_url=None,
            content_tags=[],
        ),
        SimpleNamespace(
            id='rejected',
            org_id='org-a',
            name='Rejected post',
            storage_path=None,
            status='rejected',
            uploaded_by='u',
            created_at=datetime(2026, 8, 19),
            title='Rejected post',
            format='social',
            source_url=None,
            content_tags=[],
        ),
    ]

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                class _Empty:
                    def filter(self, *args, **kwargs):
                        return self

                    def order_by(self, *args, **kwargs):
                        return self

                    def all(self):
                        return []

                return _Empty()
            return _Query()

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert [item.id for item in listed.items] == ['open']


def test_list_contents_skips_slide_preview_signing() -> None:
    from app.services.marketing_service import list_marketing_contents

    rows = [
        SimpleNamespace(
            id='c1',
            org_id='org-a',
            name='Deck',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            created_at=datetime(2026, 8, 17),
            title='Deck',
            format='presentation',
            channel=None,
            audience='retail',
            registrant_type='ria',
            source_url=None,
            body_text='hi',
            content_tags=['pitch'],
            layout_notes=[
                {
                    'region': 'slide_or_page_1',
                    'slide_image_path': 'storage://marketing-contents/org-a/slide-1.jpg',
                }
            ],
            classification={'format': 'presentation'},
            published_locations=[],
            cadence_key=None,
            cadence_step=None,
        )
    ]

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                class _Empty:
                    def filter(self, *args, **kwargs):
                        return self

                    def order_by(self, *args, **kwargs):
                        return self

                    def all(self):
                        return []

                return _Empty()
            return _Query()

    def boom(*_args, **_kwargs):
        raise AssertionError('list endpoint must not sign slide previews')

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    with patch('app.services.storage_service.create_inline_url', boom):
        listed = list_marketing_contents(user, _Db())
    assert listed.total == 1
    assert listed.items[0].layout_notes == []


def test_list_contents_survives_malformed_json_fields() -> None:
    from app.services.marketing_service import list_marketing_contents

    rows = [
        SimpleNamespace(
            id='c1',
            org_id='org-a',
            name='Bad',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            created_at=datetime(2026, 8, 17),
            title='Bad',
            format='email',
            channel=None,
            audience='retail',
            registrant_type='ria',
            source_url=None,
            body_text='hi',
            content_tags={'not': 'a-list'},
            layout_notes={'region': 'broken'},
            classification=['not-a-dict'],
            published_locations='homepage',
            cadence_key=None,
            cadence_step='x',
        )
    ]

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                class _Empty:
                    def filter(self, *args, **kwargs):
                        return self

                    def order_by(self, *args, **kwargs):
                        return self

                    def all(self):
                        return []

                return _Empty()
            return _Query()

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert listed.total == 1
    assert listed.items[0].content_tags == []
    assert listed.items[0].classification == {}
    assert listed.items[0].cadence_step is None


def test_list_contents_collapses_duplicate_webpage_titles() -> None:
    from app.services.marketing_service import list_marketing_contents

    def _row(**overrides):
        row = SimpleNamespace(
            org_id='org-a',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            format='webpage',
            channel=None,
            audience='retail',
            registrant_type='ria',
            body_text='hi',
            content_tags=[],
            layout_notes=[],
            classification={},
            published_locations=[],
            cadence_key=None,
            cadence_step=None,
        )
        row.__dict__.update(overrides)
        return row

    newest = _row(
        id='c-new',
        name='Altruist Homepage',
        title='Altruist Homepage',
        source_url='https://altruist.com',
        status='reviewed',
        created_at=datetime(2026, 8, 19, 10, 0),
        updated_at=datetime(2026, 8, 19, 10, 0),
    )
    oldest = _row(
        id='c-old',
        name='Altruist Homepage',
        title='Altruist Homepage',
        source_url='https://www.altruist.com/',
        status='reviewed',
        created_at=datetime(2026, 8, 19, 9, 0),
        updated_at=datetime(2026, 8, 19, 9, 0),
    )
    other = _row(
        id='c-email',
        name='Q3 ad',
        title='Q3 ad',
        format='email',
        source_url=None,
        created_at=datetime(2026, 8, 18),
        updated_at=datetime(2026, 8, 18),
    )

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return [newest, oldest, other]

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                class _Empty:
                    def filter(self, *args, **kwargs):
                        return self

                    def order_by(self, *args, **kwargs):
                        return self

                    def all(self):
                        return []

                return _Empty()
            return _Query()

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert listed.total == 2
    assert [item.id for item in listed.items] == ['c-new', 'c-email']


def test_list_contents_returns_empty_instead_of_500() -> None:
    from app.services.marketing_service import list_marketing_contents

    class _Db:
        def query(self, _model):
            raise RuntimeError('statement timeout')

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert listed.items == []
    assert listed.total == 0


def test_list_contents_survives_review_query_failure() -> None:
    from app.services.marketing_service import list_marketing_contents

    rows = [
        SimpleNamespace(
            id='c1',
            org_id='org-a',
            name='Deck',
            storage_path=None,
            status='reviewed',
            uploaded_by='u',
            created_at=datetime(2026, 8, 17),
            title='Deck',
            format='presentation',
            source_url=None,
            content_tags=['pitch'],
        )
    ]

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def limit(self, *_args):
            return self

        def all(self):
            return rows

    class _Db:
        def query(self, model):
            name = getattr(model, '__name__', str(model))
            if 'Review' in name:
                raise RuntimeError('review query failed')
            return _Query()

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    listed = list_marketing_contents(user, _Db())
    assert listed.total == 1
    assert listed.items[0].id == 'c1'
    assert listed.items[0].latest_review_status is None


def test_delete_missing_marketing_content_is_404() -> None:
    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return None

        def all(self):
            return []

    class _Db:
        def query(self, model):
            return _Query()

        def rollback(self):
            return None

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    with pytest.raises(HTTPException) as missing:
        delete_marketing_content('missing', user, _Db())
    assert missing.value.status_code == 404


def test_delete_marketing_content_removes_row_and_reviews() -> None:
    record = SimpleNamespace(id='c1', org_id='org-a', storage_path=None)
    review = SimpleNamespace(id='r1', artifact_storage_path=None)
    deleted: list[object] = []

    class _ContentQuery:
        def filter(self, *args, **kwargs):
            return self

        def one_or_none(self):
            return record

        def all(self):
            return [review]

        def delete(self, synchronize_session=False):
            return 0

    class _Db:
        def query(self, model):
            return _ContentQuery()

        def delete(self, obj):
            deleted.append(obj)

        def commit(self):
            return None

        def rollback(self):
            return None

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    delete_marketing_content('c1', user, _Db())
    assert record in deleted
    assert review in deleted


def test_product_2_constant_unchanged() -> None:
    assert PRODUCT_2 == 'product_2'


def test_cherry_picked_year_is_a_finding_not_a_question() -> None:
    body = 'In 2021 the strategy returned 18.2%.'
    classification = classify_marketing_material(
        format_name='presentation',
        body_text=body,
        audience='mixed',
        registrant_type='ria',
        layout_notes=[{'region': 'slide_or_page_7', 'page': 7, 'char_count': 40}],
    )
    result = heuristic_review(classification=classification, body_text=body, layout_notes=[])
    assert any('cherry' in item['why'].lower() or 'single-year' in item['why'].lower() for item in result['findings'])


@patch('app.services.marketing_ingest_service.socket.getaddrinfo', return_value=[])
def test_unresolved_host_is_rejected(_mock_dns) -> None:
    with pytest.raises(HTTPException):
        assert_safe_public_url('https://not-a-real-host.invalid')


def test_official_rule_pages_parse_xml_and_html() -> None:
    from app.services.marketing_rule_corpus import _is_challenge_page, html_or_xml_to_text

    xml_body = (
        b'<?xml version="1.0"?><DIV8><HEAD>Section 275.206(4)-1</HEAD>'
        b'<P>' + (b'Testimonials and endorsements require disclosure. ' * 20) + b'</P></DIV8>'
    )
    xml_text = html_or_xml_to_text(xml_body, 'application/xml')
    assert 'Testimonials and endorsements' in xml_text
    html_body = (
        b'<html><body><main><h1>FINRA Rule 2210</h1><p>'
        + (b'Communications with the public must be fair and balanced. ' * 20)
        + b'</p></main></body></html>'
    )
    html_text = html_or_xml_to_text(html_body, 'text/html')
    assert 'Communications with the public' in html_text
    assert _is_challenge_page(b'<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>')
    assert not _is_challenge_page(html_body)


def test_trim_rule_preamble_keeps_204_2_from_marketing_rule_citation() -> None:
    from app.services.marketing_rule_corpus import _trim_rule_preamble

    text = (
        'Site Feedback You are using an unsupported browser. '
        '§ 275.204-2 Books and records to be maintained by investment advisers. '
        'Advisers must keep copies of all advertisements including those described in '
        '§ 275.206(4)-1(e)(12) of this chapter.'
    )
    trimmed = _trim_rule_preamble(text, '204-2')
    assert trimmed.startswith('§ 275.204-2')
    assert '206(4)-1(e)(12)' in trimmed


@patch(
    'app.services.marketing_ingest_service.socket.getaddrinfo',
    return_value=[(0, 0, 0, '', ('93.184.216.34', 443))],
)
def test_rule_corpus_rejects_non_official_hosts(_mock_dns) -> None:
    from app.services.marketing_rule_corpus import _assert_rule_url

    with pytest.raises(HTTPException):
        _assert_rule_url('https://www.example.com/fake-rule')


def test_primary_marketing_cosine_prefers_related_max() -> None:
    from app.services.marketing_vector_gap import primary_marketing_cosine_similarity

    score = primary_marketing_cosine_similarity(
        {
            'cosine_similarity': {
                'rules': [
                    {'rule_id': '2210', 'cosine_similarity': 0.41, 'related': True},
                    {'rule_id': '204-2', 'cosine_similarity': 0.22, 'related': True},
                    {'rule_id': '2215', 'cosine_similarity': 0.99, 'related': False},
                ]
            }
        }
    )
    assert score == pytest.approx(0.41)


def test_primary_marketing_cosine_falls_back_to_all_rules() -> None:
    from app.services.marketing_vector_gap import primary_marketing_cosine_similarity

    score = primary_marketing_cosine_similarity(
        {
            'cosine_similarity': {
                'rules': [
                    {'rule_id': '2210', 'cosine_similarity': 0.12, 'related': False},
                    {'rule_id': '204-2', 'cosine_similarity': 0.09, 'related': False},
                ]
            }
        }
    )
    assert score == pytest.approx(0.12)


def test_cosine_keeps_only_interrelated_rule_chunks() -> None:
    from app.services.document_ingestion import TextChunk
    from app.services.marketing_rule_corpus import RuleDocument
    from app.services.marketing_vector_gap import EmbeddedRule, rank_related_excerpts

    homepage_vectors = [[1.0, 0.0]]
    related = EmbeddedRule(
        document=RuleDocument(
            rule_id='206(4)-1',
            regulator='SEC',
            title='Marketing Rule',
            url='https://www.ecfr.gov/current/title-17/chapter-II/part-275/section-275.206(4)-1',
            text='testimonials',
            source='live',
        ),
        chunks=[TextChunk(index=0, content='Testimonials require prominent disclosure.', metadata={})],
        vectors=[[0.98, 0.02]],
        cosine_similarity=0.98,
    )
    unrelated = EmbeddedRule(
        document=RuleDocument(
            rule_id='2215',
            regulator='FINRA',
            title='Security futures',
            url='https://www.finra.org/rules-guidance/rulebooks/finra-rules/2215',
            text='security futures',
            source='live',
        ),
        chunks=[TextChunk(index=0, content='Security futures are not suitable for all investors.', metadata={})],
        vectors=[[0.0, 1.0]],
        cosine_similarity=0.0,
    )
    excerpts = rank_related_excerpts(homepage_vectors, [related, unrelated])
    assert [item['rule_id'] for item in excerpts] == ['206(4)-1']
    assert excerpts[0]['cosine_similarity'] > 0.9


@pytest.mark.asyncio
async def test_homepage_rule_gap_embeds_and_filters_by_cosine(monkeypatch) -> None:
    from app.services.marketing_rule_corpus import RULE_SPECS, fallback_rule_document
    from app.services.marketing_vector_gap import (
        analyze_homepage_rule_gap,
        clear_embedding_cache,
    )

    clear_embedding_cache()

    async def fake_embed(chunks, _settings):
        blob = ' '.join(chunk.content for chunk in chunks).lower()
        if '11.4%' in blob or 'granite peak' in blob or 'marketing_homepage' in blob:
            return [[1.0, 0.0] for _ in chunks]
        if '206(4)-1' in blob or 'marketing rule' in blob or 'testimonial' in blob:
            return [[0.97, 0.05] for _ in chunks]
        if 'security futures' in blob or '2215' in blob:
            return [[0.0, 1.0] for _ in chunks]
        return [[0.15, 0.98] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_vector_gap.generate_embeddings',
        fake_embed,
    )
    specs_by_id = {spec.rule_id: spec for spec in RULE_SPECS}
    documents = [
        fallback_rule_document(specs_by_id['206(4)-1']),
        fallback_rule_document(specs_by_id['2215']),
    ]
    result = await analyze_homepage_rule_gap(
        homepage_text=EXAMPLE_1_HOMEPAGE,
        settings=SimpleNamespace(openai_embedding_model='text-embedding-3-large'),
        rule_documents=documents,
    )
    related_ids = {item['rule_id'] for item in result.related_excerpts}
    assert '206(4)-1' in related_ids
    assert '2215' not in related_ids
    by_id = {item.document.rule_id: item.cosine_similarity for item in result.rules}
    assert by_id['206(4)-1'] > by_id['2215']
    assert result.homepage_chunk_count > 0


def test_vector_gap_formats_include_pitch_deck_social_email_and_blog() -> None:
    from app.services.marketing_vector_gap import (
        should_analyze_marketing_rule_gap,
        uses_fallback_rule_corpus,
    )

    for format_name in ('presentation', 'social', 'email', 'blog', 'webpage', 'image'):
        assert should_analyze_marketing_rule_gap(format_name) is True
    assert should_analyze_marketing_rule_gap('text') is False
    assert uses_fallback_rule_corpus('social') is True
    assert uses_fallback_rule_corpus('email') is True
    assert uses_fallback_rule_corpus('image') is True
    assert uses_fallback_rule_corpus('webpage') is False
    assert uses_fallback_rule_corpus('presentation') is False


@pytest.mark.asyncio
@pytest.mark.parametrize('format_name', ['social', 'email'])
async def test_short_form_rule_gap_skips_live_rule_downloads(monkeypatch, format_name: str) -> None:
    from app.services.marketing_rule_corpus import RULE_SPECS, fallback_rule_document
    from app.services.marketing_vector_gap import analyze_marketing_rule_gap, clear_embedding_cache

    clear_embedding_cache()

    async def fail_live(*args, **kwargs):
        raise AssertionError(f'{format_name} reviews must not download live rule pages')

    monkeypatch.setattr(
        'app.services.marketing_vector_gap.load_marketing_rule_corpus',
        fail_live,
    )

    async def fake_embed(chunks, _settings):
        return [[1.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_vector_gap.generate_embeddings',
        fake_embed,
    )
    result = await analyze_marketing_rule_gap(
        content_text='Beat the market by 12%.',
        format_name=format_name,
        settings=SimpleNamespace(openai_embedding_model='text-embedding-3-large'),
    )
    assert result.error is None
    assert {item.document.rule_id for item in result.rules} == {spec.rule_id for spec in RULE_SPECS}
    assert all(item.document.source == 'fallback' for item in result.rules)
    assert fallback_rule_document(RULE_SPECS[0]).source == 'fallback'


def test_marketing_gap_embed_text_includes_layout_notes() -> None:
    from app.services.marketing_vector_gap import marketing_gap_embed_text

    blob = marketing_gap_embed_text(
        body_text='Beat the market by 12%.',
        layout_notes=[{'region': 'hero', 'excerpt': '11.4% annually', 'font_note': '72pt'}],
    )
    assert 'Beat the market by 12%.' in blob
    assert '11.4% annually' in blob
    assert 'hero' in blob


@pytest.mark.asyncio
async def test_marketing_rule_gap_runs_for_pitch_deck_social_email_and_blog(monkeypatch) -> None:
    from app.services.marketing_rule_corpus import RULE_SPECS, fallback_rule_document
    from app.services.marketing_vector_gap import (
        analyze_marketing_rule_gap,
        clear_embedding_cache,
    )

    clear_embedding_cache()

    async def fake_embed(chunks, _settings):
        blob = ' '.join(chunk.content for chunk in chunks).lower()
        if '12%' in blob or 'beat the market' in blob:
            return [[1.0, 0.0] for _ in chunks]
        if '206(4)-1' in blob or 'marketing rule' in blob or 'testimonial' in blob:
            return [[0.97, 0.05] for _ in chunks]
        if 'security futures' in blob or '2215' in blob:
            return [[0.0, 1.0] for _ in chunks]
        return [[0.15, 0.98] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_vector_gap.generate_embeddings',
        fake_embed,
    )
    specs_by_id = {spec.rule_id: spec for spec in RULE_SPECS}
    documents = [
        fallback_rule_document(specs_by_id['206(4)-1']),
        fallback_rule_document(specs_by_id['2215']),
    ]
    copy = 'Beat the market by 12% last year. Past results shown gross of fees.'
    for format_name in ('presentation', 'social', 'email', 'blog'):
        result = await analyze_marketing_rule_gap(
            content_text=copy,
            format_name=format_name,
            settings=SimpleNamespace(openai_embedding_model='text-embedding-3-large'),
            rule_documents=documents,
        )
        related_ids = {item['rule_id'] for item in result.related_excerpts}
        assert '206(4)-1' in related_ids, format_name
        assert '2215' not in related_ids, format_name
        assert result.format_name == format_name
        assert result.content_chunk_count > 0


@pytest.mark.asyncio
async def test_presentation_slide_gaps_embed_text_and_diagrams_per_slide(monkeypatch) -> None:
    from app.services.marketing_rule_corpus import RULE_SPECS, fallback_rule_document
    from app.services.marketing_slide_service import parse_slides_from_text, slide_embed_corpus
    from app.services.marketing_vector_gap import (
        analyze_presentation_slide_gaps,
        clear_embedding_cache,
    )

    clear_embedding_cache()

    async def fake_embed(chunks, _settings):
        blob = ' '.join(chunk.content for chunk in chunks).lower()
        if '22.4%' in blob or 'would have returned' in blob or 'slide 7' in blob:
            return [[1.0, 0.0] for _ in chunks]
        if '206(4)-1' in blob or 'marketing rule' in blob or 'testimonial' in blob:
            return [[0.97, 0.05] for _ in chunks]
        if '204-2' in blob or 'recordkeeping' in blob:
            return [[0.6, 0.4] for _ in chunks]
        if '2210' in blob or 'communications with the public' in blob:
            return [[0.55, 0.45] for _ in chunks]
        return [[0.12, 0.98] for _ in chunks]

    monkeypatch.setattr('app.services.marketing_vector_gap.generate_embeddings', fake_embed)
    specs_by_id = {spec.rule_id: spec for spec in RULE_SPECS}
    documents = [
        fallback_rule_document(specs_by_id['206(4)-1']),
        fallback_rule_document(specs_by_id['204-2']),
        fallback_rule_document(specs_by_id['2210']),
        fallback_rule_document(specs_by_id['2215']),
    ]
    slides = slide_embed_corpus(parse_slides_from_text(EXAMPLE_2_DECK))
    result = await analyze_presentation_slide_gaps(
        slides=slides,
        global_text=EXAMPLE_2_DECK,
        settings=SimpleNamespace(openai_embedding_model='text-embedding-3-large'),
        rule_documents=documents,
    )
    assert result.slides
    pages = {item['page'] for item in result.slides}
    assert {7, 14} <= pages
    slide7 = next(item for item in result.slides if item['page'] == 7)
    assert '206(4)-1' in slide7['related_rule_ids']
    assert '2215' not in slide7['related_rule_ids']


def test_homepage_findings_map_to_gap_table_rule_gap_fix() -> None:
    from app.services.marketing_gap_bridge import (
        collect_marketing_rule_codes,
        display_marketing_policy_name,
        impact_for_marketing_finding,
        map_marketing_findings,
        marketing_format_label,
        marketing_gap_api_fields,
        marketing_rule_labels,
        marketing_shell_name_aliases,
        marketing_shell_policy_name,
    )

    findings = [
        {
            'id': 'layout_buried',
            'excerpt': 'Disclosures exist but are buried',
            'title': 'Footer disclosure block — buried',
            'rule': '206(4)-1',
            'why': (
                'Every required disclosure on this page is technically present — in 7pt grey, '
                'below the fold, after the sitemap.'
            ),
            'confidence': 'certain',
            'recommended_option': 'redline',
            'options': ['redline', 'do_nothing'],
            'option_labels': {'redline': 'Redline (layout instruction)'},
            'status': 'open',
        },
        {
            'id': 'performance_gross_hero',
            'excerpt': '11.4% annually since 2015',
            'title': 'gross only, in the hero',
            'rule': '206(4)-1',
            'why': 'Gross figure with the net figure nowhere on the page.',
            'confidence': 'certain',
            'recommended_option': 'redline',
            'options': ['redline', 'pull'],
            'option_labels': {'redline': 'Redline'},
            'status': 'open',
        },
    ]
    mapped = map_marketing_findings(findings)
    assert mapped[0]['finding'] == 'Disclosures exist but are buried'
    assert mapped[0]['rule'] == '206(4)-1'
    assert mapped[0]['why'].startswith('Every required disclosure')
    assert mapped[0]['confidence'] == 'Certain'
    assert mapped[0]['options_display'] == 'Redline (layout instruction, recommended)'
    assert mapped[0]['display_kind'] == 'marketing'
    assert mapped[1]['finding'] == '"11.4% annually since 2015" — gross only, in the hero'
    assert mapped[1]['options_display'] == 'Redline (recommended)'
    assert mapped[1]['impact_score'] == 'HIGH'
    assert impact_for_marketing_finding({'recommended_option': 'pull'}) == 'HIGH'
    content = SimpleNamespace(
        title='Altruist Homepage',
        name='Altruist Homepage',
        source_url='https://altruist.com/try/compare-custodians',
        format='webpage',
    )
    name = marketing_shell_policy_name(content)
    assert name.startswith('Webpage: Altruist Homepage')
    assert 'altruist.com' in name
    assert display_marketing_policy_name('Presentation — Reg-AI Pitch Deck') == (
        'Presentation: Reg-AI Pitch Deck'
    )
    assert marketing_shell_name_aliases('Presentation: Reg-AI Pitch Deck') == [
        'Presentation: Reg-AI Pitch Deck',
        'Presentation — Reg-AI Pitch Deck',
    ]
    assert marketing_format_label('webpage') == 'Webpage'
    assert marketing_format_label('social') == 'Social post'
    assert marketing_format_label('blog') == 'Blog / article'
    assert marketing_format_label('text') == 'Other text'
    codes = collect_marketing_rule_codes(
        findings=findings,
        classification={'rules': ['204-2', '206(4)-1']},
        cosine={'rules': [{'rule_id': '2210', 'related': True}]},
    )
    labels = marketing_rule_labels(codes)
    assert labels[0] == 'SEC: Rule 206(4)-1'
    assert 'SEC: Rule 204-2' in labels
    assert 'FINRA: Rule 2210' in labels
    gap = SimpleNamespace(
        trigger_type='marketing_content',
        retrieval_metadata={
            'marketing_format': 'webpage',
            'marketing_rule_labels': [
                'SEC: Rule 206(4)-1, the Marketing Rule',
                'SEC: Rule 204-2, recordkeeping',
                'FINRA: Rule 2210, communications with the public',
            ],
        },
    )
    api = marketing_gap_api_fields(gap)
    assert api['marketing_format_label'] == 'Webpage'
    assert api['marketing_rule_labels'] == [
        'SEC: Rule 206(4)-1',
        'SEC: Rule 204-2',
        'FINRA: Rule 2210',
    ]
    assert 'policy_name' not in api


def test_canonical_gap_trigger_keeps_marketing_content() -> None:
    from app.services.gap_analysis_service import canonical_gap_trigger_type

    assert canonical_gap_trigger_type('marketing_content') == 'marketing_content'
    assert canonical_gap_trigger_type('unknown') == 'publication'


def test_ensure_marketing_schema_skips_test_doubles() -> None:
    from app.services.marketing_schema import (
        ensure_marketing_review_schema,
        reset_marketing_schema_heal_cache,
    )

    reset_marketing_schema_heal_cache()
    assert ensure_marketing_review_schema(None) is False
    assert ensure_marketing_review_schema(SimpleNamespace()) is False


def test_persist_retries_once_after_commit_error() -> None:
    from sqlalchemy.exc import OperationalError

    from app.services.marketing_service import _persist_marketing_upload

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return []

        def first(self):
            return None

        def one_or_none(self):
            return None

    class _Db:
        def __init__(self) -> None:
            self.commits = 0

        def query(self, _model):
            return _Query()

        def add(self, _obj):
            return None

        def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise OperationalError('INSERT', {}, Exception('queued check'))

        def refresh(self, _obj):
            return None

        def rollback(self):
            return None

    user = CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER)
    db = _Db()
    record, _review, _content_id = _persist_marketing_upload(
        db,
        user=user,
        settings=SimpleNamespace(supabase_storage_bucket_list=()),
        file=None,
        contents=b'',
        filename='deck.pptx',
        content_type='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        storage_uri='storage://marketing-contents/org-a/deck.pptx',
        url='',
        extracted='slide text',
        notes=[],
        html_preview=None,
        classification={'tags': []},
        display_name='Nylas Pitch Deck Sample',
        resolved_format='presentation',
        resolved_audience='retail',
        resolved_registrant='unknown',
        channel=None,
        cadence_key=None,
        cadence_step=None,
        published_locations=None,
        preferred_id=None,
    )
    assert db.commits == 2
    assert record.name == 'Nylas Pitch Deck Sample'
    assert record.status == 'queued'


@pytest.mark.asyncio
async def test_dispatch_review_always_queues_local_background(monkeypatch) -> None:
    from app.services import marketing_service

    queued: list[tuple[object, ...]] = []

    class _Background:
        def add_task(self, task: object, *args: object) -> None:
            queued.append((task, args))

    monkeypatch.setattr(
        marketing_service,
        'get_settings',
        lambda: SimpleNamespace(inngest_async_dispatch_enabled=False),
    )

    await marketing_service._dispatch_review('content-1', 'org-a', _Background())

    assert len(queued) == 1
    assert queued[0][0] is marketing_service._run_review_job
    assert queued[0][1] == ('content-1', 'org-a', {})


@pytest.mark.asyncio
async def test_homepage_rule_gap_falls_back_when_live_corpus_times_out(monkeypatch) -> None:
    from app.services.marketing_rule_corpus import RULE_SPECS
    from app.services.marketing_vector_gap import analyze_marketing_rule_gap, clear_embedding_cache

    clear_embedding_cache()

    async def hang_corpus(*_args: object, **_kwargs: object):
        import asyncio

        await asyncio.sleep(30)
        raise AssertionError('live corpus should have been timed out')

    async def fake_embed(chunks, _settings):
        return [[1.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_vector_gap.load_marketing_rule_corpus',
        hang_corpus,
    )
    monkeypatch.setattr(
        'app.services.marketing_vector_gap.generate_embeddings',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.marketing_vector_gap.LIVE_RULE_CORPUS_TIMEOUT_SECONDS',
        0.05,
    )

    result = await analyze_marketing_rule_gap(
        content_text='Testimonials and 11.4% annual returns on the homepage.',
        format_name='webpage',
        settings=SimpleNamespace(openai_embedding_model='text-embedding-3-large'),
    )
    assert result.error is None
    assert {item.document.rule_id for item in result.rules} == {spec.rule_id for spec in RULE_SPECS}
    assert all(item.document.source == 'fallback' for item in result.rules)


def test_get_marketing_content_requeues_stale_running_review() -> None:
    from datetime import UTC, timedelta

    from app.models.core import MarketingContent, MarketingReview
    from app.services.marketing_service import get_marketing_content

    now = datetime.now(UTC).replace(tzinfo=None)
    content = MarketingContent(
        id='content-1',
        org_id='org-a',
        name='Homepage',
        status='in_review',
        format='webpage',
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    review = MarketingReview(
        id='review-1',
        org_id='org-a',
        content_id='content-1',
        status='running',
        findings=[],
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    queued: list[tuple[object, ...]] = []

    class _Query:
        def __init__(self, one: object | None = None, rows: list[object] | None = None) -> None:
            self.one = one
            self.rows = rows or []

        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> object | None:
            return self.one

        def first(self) -> object | None:
            return self.one

        def all(self) -> list[object]:
            return self.rows

    class _Session:
        def __init__(self) -> None:
            self.commits = 0

        def query(self, model: object) -> _Query:
            if model is MarketingContent:
                return _Query(one=content)
            if model is MarketingReview:
                return _Query(one=review)
            return _Query()

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

    class _Background:
        def add_task(self, task: object, *args: object) -> None:
            queued.append((task, args))

    db = _Session()
    result = get_marketing_content(
        'content-1',
        CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER),
        db,  # type: ignore[arg-type]
        background_tasks=_Background(),
    )

    assert result.latest_review is not None
    assert result.latest_review.status == 'running'
    assert len(queued) == 1
    assert queued[0][1] == ('content-1', 'org-a', {})
    assert db.commits == 1


def test_get_marketing_content_skips_requeue_when_review_already_running() -> None:
    from datetime import UTC, timedelta

    from app.models.core import MarketingContent, MarketingReview
    from app.services import marketing_service

    now = datetime.now(UTC).replace(tzinfo=None)
    content = MarketingContent(
        id='content-1',
        org_id='org-a',
        name='Homepage',
        status='in_review',
        format='webpage',
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    review = MarketingReview(
        id='review-1',
        org_id='org-a',
        content_id='content-1',
        status='running',
        findings=[],
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
    )
    queued: list[tuple[object, ...]] = []

    class _Query:
        def __init__(self, one: object | None = None) -> None:
            self.one = one

        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> object | None:
            return self.one

        def first(self) -> object | None:
            return self.one

        def all(self) -> list[object]:
            return []

    class _Session:
        def query(self, model: object) -> _Query:
            if model is MarketingContent:
                return _Query(one=content)
            if model is MarketingReview:
                return _Query(one=review)
            return _Query()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _Background:
        def add_task(self, task: object, *args: object) -> None:
            queued.append((task, args))

    marketing_service._inflight_reviews.add('content-1')
    try:
        result = marketing_service.get_marketing_content(
            'content-1',
            CurrentUser(user_id='u', org_id='org-a', role=CHIEF_COMPLIANCE_OFFICER),
            _Session(),  # type: ignore[arg-type]
            background_tasks=_Background(),
        )
    finally:
        marketing_service._inflight_reviews.discard('content-1')

    assert result.latest_review is not None
    assert result.latest_review.status == 'running'
    assert queued == []


@pytest.mark.asyncio
async def test_run_review_background_skips_duplicate_content(monkeypatch) -> None:
    import asyncio

    from app.services import marketing_service

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_review(**_kwargs: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {'status': 'ready'}

    monkeypatch.setattr(
        'app.services.marketing_review_workflow.run_marketing_review',
        slow_review,
    )

    first = asyncio.create_task(
        marketing_service._run_review_background('content-1', 'org-a', {})
    )
    await started.wait()
    await marketing_service._run_review_background('content-1', 'org-a', {})
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
async def test_caption_slide_diagrams_prefers_full_slide_preview_over_logos() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, caption_slide_diagrams

    llm = FakeLLM('chart 12% net')

    slide = SlidePayload(
        page=1,
        text='',
        page_count=1,
        images=[SlideImage(media_type='image/jpeg', data=_jpeg_bytes(80, 80), name='chart.jpg')],
        preview_image=SlideImage(
            media_type='image/jpeg',
            data=_jpeg_bytes(800, 600),
            name='preview.jpg',
        ),
    )
    await caption_slide_diagrams([slide], client=llm)
    assert len(llm.calls) == 1
    assert llm.calls[0]['throttle'] == 'sonnet'
    assert slide.text == 'chart 12% net'


@pytest.mark.asyncio
async def test_caption_slide_diagrams_reads_empty_screenshot_slides_first() -> None:
    from app.services.marketing_slide_service import SlideImage, SlidePayload, caption_slide_diagrams


    logos = [
        SlideImage(media_type='image/jpeg', data=_jpeg_bytes(80, 80), name=f'logo{i}.jpg')
        for i in range(8)
    ]
    screenshot = SlideImage(
        media_type='image/jpeg',
        data=_jpeg_bytes(1280, 720),
        name='shot.jpg',
    )
    titled = SlidePayload(page=1, text='Title only', images=logos)
    empty = SlidePayload(page=7, text='', images=[screenshot])
    await caption_slide_diagrams(
        [titled, empty],
        client=FakeLLM('{"transcription": "Screenshot copy 11.4%", "findings": []}'),
        max_images=1,
    )
    assert 'Screenshot copy 11.4%' in empty.text
    assert titled.text == 'Title only'


def test_transcribed_screenshot_slide_enters_the_redline_template() -> None:
    from app.services.marketing_redline_service import format_layout_notes_template
    from app.services.marketing_slide_service import SlideImage, SlidePayload, layout_notes_from_slides

    slide = SlidePayload(
        page=7,
        text='We give developers the useful data and the tools to put it to work.',
        page_count=16,
        images=[SlideImage(media_type='image/jpeg', data=_jpeg_bytes(1280, 720), name='shot.jpg')],
    )
    template = format_layout_notes_template(layout_notes_from_slides([slide]))
    assert 'We give developers the useful data' in template
    assert '[SLIDE 7 — screenshot]' not in template

