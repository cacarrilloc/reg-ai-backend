from pydantic import BaseModel, Field

from app.services.llm_json import coerce_string_fields, response_model, truncate_text


class SamplePayload(BaseModel):
    summary: str = Field(min_length=1, max_length=100)


def test_truncate_text_adds_ellipsis() -> None:
    assert truncate_text('abcdefghij', 7) == 'abcd...'


def test_coerce_string_fields_truncates_values() -> None:
    payload = coerce_string_fields({'summary': 'x' * 150}, {'summary': 100})

    assert len(payload['summary']) == 100
    assert payload['summary'].endswith('...')


def test_response_model_coerces_overlong_strings() -> None:
    payload = response_model(
        '{"summary": "' + ('z' * 150) + '"}',
        SamplePayload,
        string_field_limits={'summary': 100},
    )

    assert len(payload.summary) == 100
