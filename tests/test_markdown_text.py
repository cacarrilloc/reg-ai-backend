from app.services.markdown_text import strip_markdown_emphasis


def test_strip_markdown_emphasis_removes_bold_markers() -> None:
    text = strip_markdown_emphasis(
        '**Business Change:** Hire an AML attorney. **Compliance Impact:** Update WSPs.'
    )
    assert text == 'Business Change: Hire an AML attorney. Compliance Impact: Update WSPs.'
    assert '**' not in text
