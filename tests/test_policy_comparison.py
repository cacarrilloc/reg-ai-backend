import pytest

from app.models.core import DocumentChunk, Regulation
from app.services.policy_comparison_service import (
    GovernmentExcerpt,
    load_regulatory_reference_attachments,
    merge_government_excerpts,
    retrieve_government_excerpts_for_business_change,
)
from app.services.policy_types import REGULATORY_REFERENCE_TYPE


def gov_excerpt(
    *,
    chunk_id: str,
    regulator_source: str,
    similarity: float,
) -> GovernmentExcerpt:
    return GovernmentExcerpt(
        policy_id=f'policy-{regulator_source.lower()}',
        policy_name=f'{regulator_source} Newsroom Reference',
        regulator_source=regulator_source,
        chunk_id=chunk_id,
        chunk_index=0,
        content=f'{regulator_source} excerpt text.',
        similarity=similarity,
    )


def test_merge_government_excerpts_deduplicates_by_chunk_id() -> None:
    first = gov_excerpt(chunk_id='chunk-a', regulator_source='SEC', similarity=0.7)
    duplicate = gov_excerpt(chunk_id='chunk-a', regulator_source='SEC', similarity=0.9)
    second = gov_excerpt(chunk_id='chunk-b', regulator_source='FINRA', similarity=0.6)

    merged = merge_government_excerpts([first, duplicate], [second])

    assert [excerpt.chunk_id for excerpt in merged] == ['chunk-a', 'chunk-b']
    assert merged[0].similarity == 0.9


@pytest.mark.asyncio
async def test_retrieve_government_excerpts_for_business_change_adds_missing_regulator(
    monkeypatch,
) -> None:
    async def fake_retrieve_for_text(*_args, **_kwargs):
        return [gov_excerpt(chunk_id='chunk-sec', regulator_source='SEC', similarity=0.8)]

    async def fake_top_for_regulator(_db, _org_id, regulator_source, *_args, **_kwargs):
        if regulator_source == 'FINRA':
            return gov_excerpt(chunk_id='chunk-finra', regulator_source='FINRA', similarity=0.5)
        return None

    monkeypatch.setattr(
        'app.services.policy_comparison_service.retrieve_government_excerpts_for_text',
        fake_retrieve_for_text,
    )
    monkeypatch.setattr(
        'app.services.policy_comparison_service.top_government_excerpt_for_regulator',
        fake_top_for_regulator,
    )

    excerpts = await retrieve_government_excerpts_for_business_change(
        None,
        'org_example',
        'Launching a new savings account product.',
        SettingsStub(),
    )

    sources = {excerpt.regulator_source for excerpt in excerpts}
    assert sources == {'SEC', 'FINRA'}


class SettingsStub:
    openai_api_key = ''


def test_load_regulatory_reference_attachments_loads_all_active_policies() -> None:
    finra_policy = Regulation(
        id='policy-finra',
        org_id='org_example',
        name='FINRA Media Center Reference',
        type=REGULATORY_REFERENCE_TYPE,
        status='ACTIVE',
        version=2,
        file_url='https://www.finra.org/media-center',
    )
    sec_policy = Regulation(
        id='policy-sec',
        org_id='org_example',
        name='SEC Newsroom Reference',
        type=REGULATORY_REFERENCE_TYPE,
        status='ACTIVE',
        version=1,
        file_url='https://www.sec.gov/newsroom',
    )
    finra_chunks = [
        DocumentChunk(
            id='chunk-finra-1',
            policy_id='policy-finra',
            chunk_index=0,
            content='FINRA chunk one.',
        ),
        DocumentChunk(
            id='chunk-finra-2',
            policy_id='policy-finra',
            chunk_index=1,
            content='FINRA chunk two.',
        ),
    ]
    sec_chunks = [
        DocumentChunk(
            id='chunk-sec-1',
            policy_id='policy-sec',
            chunk_index=0,
            content='SEC supervisory guidance.',
        ),
    ]

    class Query:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object) -> object:
            return self

        def order_by(self, *_args: object) -> object:
            return self

        def all(self) -> object:
            return self.result

    class SessionStub:
        def query(self, model: object) -> Query:
            if model is Regulation:
                return Query([finra_policy, sec_policy])
            if model is DocumentChunk:
                return Query(finra_chunks if self._next_policy == 'policy-finra' else sec_chunks)
            return Query([])

        def __init__(self) -> None:
            self._next_policy = 'policy-finra'

    session = SessionStub()

    def fake_load_text(_db: object, policy: Regulation) -> str:
        if policy.id == 'policy-finra':
            session._next_policy = 'policy-finra'
            return 'FINRA chunk one.\n\nFINRA chunk two.'
        session._next_policy = 'policy-sec'
        return 'SEC supervisory guidance.'

    import app.services.policy_comparison_service as comparison_service

    original = comparison_service.load_regulatory_reference_policy_text
    comparison_service.load_regulatory_reference_policy_text = fake_load_text
    try:
        attachments = load_regulatory_reference_attachments(session, 'org_example')  # type: ignore[arg-type]
    finally:
        comparison_service.load_regulatory_reference_policy_text = original

    assert len(attachments) == 2
    assert {attachment.policy_name for attachment in attachments} == {
        'FINRA Media Center Reference',
        'SEC Newsroom Reference',
    }
    assert attachments[0].content == 'FINRA chunk one.\n\nFINRA chunk two.'
    assert attachments[1].regulator_source == 'SEC'


def test_load_regulatory_reference_attachments_deduplicates_by_name() -> None:
    older = Regulation(
        id='policy-old',
        org_id='org_example',
        name='CFTC Press Room Reference',
        type=REGULATORY_REFERENCE_TYPE,
        status='ACTIVE',
        version=1,
        file_url='https://www.cftc.gov/PressRoom',
    )
    newer = Regulation(
        id='policy-new',
        org_id='org_example',
        name='CFTC Press Room Reference',
        type=REGULATORY_REFERENCE_TYPE,
        status='ACTIVE',
        version=3,
        file_url='https://www.cftc.gov/PressRoom',
    )

    class Query:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object) -> object:
            return self

        def order_by(self, *_args: object) -> object:
            return self

        def all(self) -> object:
            return self.result

    class SessionStub:
        def query(self, model: object) -> Query:
            if model is Regulation:
                return Query([newer, older])
            return Query([])

    import app.services.policy_comparison_service as comparison_service

    comparison_service.load_regulatory_reference_policy_text = (
        lambda _db, policy: f'content for {policy.id}'
    )
    attachments = load_regulatory_reference_attachments(SessionStub(), 'org_example')  # type: ignore[arg-type]

    assert len(attachments) == 1
    assert attachments[0].policy_id == 'policy-new'
    assert attachments[0].version == 3
