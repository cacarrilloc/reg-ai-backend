from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.business_change_policy_workflow_service import (
    BusinessChangeSnapshot,
    PolicyMatchCandidate,
    build_new_policy_docx_from_business_change,
    gap_analysis_is_redline_eligible,
    match_business_change_to_internal_policies,
    proposed_policy_name_for_business_change,
    score_internal_policies_by_cosine,
)


def sample_change() -> BusinessChangeSnapshot:
    return BusinessChangeSnapshot(
        id='bc-1',
        org_id='org_example',
        change_type='NEW_PRODUCT',
        description='Launch a retail mobile trading application with electronic communications supervision.',
        event_summary='Adds mobile trading communications oversight.',
        triggers_review=True,
    )


def test_cosine_match_selects_highest_scoring_internal_policy() -> None:
    change_vectors = [[1.0, 0.0, 0.0]]
    comms = PolicyMatchCandidate(
        policy_id='policy-comms',
        policy_name='Electronic Communications Policy',
        policy_version=1,
        excerpt='Supervisors review retained electronic communications.',
        vectors=((0.96, 0.1, 0.0),),
    )
    aml = PolicyMatchCandidate(
        policy_id='policy-aml',
        policy_name='AML Policy',
        policy_version=2,
        excerpt='The firm maintains an anti-money laundering program.',
        vectors=((0.40, 0.90, 0.0),),
    )

    scored = score_internal_policies_by_cosine(change_vectors, [aml, comms])

    assert scored[0][1].policy_id == 'policy-comms'
    assert scored[0][0] > scored[1][0]


@pytest.mark.asyncio
async def test_match_uses_cosine_before_lexical_fallback() -> None:
    change = sample_change()
    candidates = [
        PolicyMatchCandidate(
            policy_id='policy-aml',
            policy_name='AML Policy',
            policy_version=2,
            excerpt='The firm maintains an anti-money laundering program for wire transfers.',
            vectors=((1.0, 0.0),),
        ),
        PolicyMatchCandidate(
            policy_id='policy-comms',
            policy_name='Electronic Communications Policy',
            policy_version=1,
            excerpt='Supervisors review retained electronic communications.',
            vectors=((0.1, 0.99),),
        ),
    ]

    result = await match_business_change_to_internal_policies(
        change,
        candidates,
        Settings(ANTHROPIC_API_KEY=''),
        client=None,
        change_vectors=[[1.0, 0.0]],
    )

    assert result.matched is True
    assert result.matched_policy_ids == ['policy-aml']
    assert result.mode == 'COSINE'
    assert result.cosine_scores[0][0] == 'policy-aml'


@pytest.mark.asyncio
async def test_fallback_match_selects_related_internal_policy() -> None:
    change = sample_change()
    candidates = [
        PolicyMatchCandidate(
            policy_id='policy-comms',
            policy_name='Electronic Communications Policy',
            policy_version=1,
            excerpt=(
                'Supervisors review retained electronic communications every month and '
                'document exceptions for mobile trading applications.'
            ),
        ),
        PolicyMatchCandidate(
            policy_id='policy-aml',
            policy_name='AML Policy',
            policy_version=2,
            excerpt='The firm maintains an anti-money laundering program.',
        ),
    ]

    result = await match_business_change_to_internal_policies(
        change,
        candidates,
        Settings(ANTHROPIC_API_KEY=''),
        client=None,
    )

    assert result.matched is True
    assert result.matched_policy_ids == ['policy-comms']
    assert result.mode == 'RULE_FALLBACK'


@pytest.mark.asyncio
async def test_fallback_match_returns_no_match_when_unrelated() -> None:
    change = sample_change()
    candidates = [
        PolicyMatchCandidate(
            policy_id='policy-aml',
            policy_name='AML Policy',
            policy_version=2,
            excerpt='The firm maintains an anti-money laundering program for wire transfers.',
        ),
    ]

    result = await match_business_change_to_internal_policies(
        change,
        candidates,
        Settings(ANTHROPIC_API_KEY=''),
        client=None,
    )

    assert result.matched is True
    assert result.matched_policy_ids == ['policy-aml']
    assert result.mode == 'BEST_AVAILABLE'


@pytest.mark.asyncio
async def test_below_cosine_floor_still_selects_closest_internal_policy() -> None:
    change = sample_change()
    candidates = [
        PolicyMatchCandidate(
            policy_id='policy-aml',
            policy_name='AML Policy',
            policy_version=2,
            excerpt='The firm maintains an anti-money laundering program for wire transfers.',
            vectors=((0.0, 1.0),),
        ),
    ]

    result = await match_business_change_to_internal_policies(
        change,
        candidates,
        Settings(ANTHROPIC_API_KEY=''),
        client=None,
        change_vectors=[[1.0, 0.0]],
    )

    assert result.matched is True
    assert result.matched_policy_ids == ['policy-aml']
    assert result.mode == 'BEST_AVAILABLE'


def test_build_new_policy_docx_from_business_change_is_docx() -> None:
    from io import BytesIO
    from zipfile import ZipFile

    change = sample_change()
    output = build_new_policy_docx_from_business_change(change)
    assert proposed_policy_name_for_business_change(change) == 'NEW_PRODUCT Policy'
    with ZipFile(BytesIO(output)) as archive:
        assert 'word/document.xml' in archive.namelist()


def test_gap_analysis_is_redline_eligible_for_matched_business_change_only() -> None:
    assert gap_analysis_is_redline_eligible(None, trigger_type='publication') is True
    assert gap_analysis_is_redline_eligible(
        {'business_change_policy_match': True, 'redline_eligible': True},
        trigger_type='business_change',
    ) is True
    assert gap_analysis_is_redline_eligible(
        {'retrieval_mode': 'VECTOR'},
        trigger_type='business_change',
    ) is True
    assert gap_analysis_is_redline_eligible(
        {'new_policy_from_business_change': True},
        trigger_type='business_change',
    ) is False
    assert gap_analysis_is_redline_eligible(None, trigger_type='marketing_content') is False
    assert gap_analysis_is_redline_eligible(
        {'redline_eligible': True},
        trigger_type='marketing_content',
    ) is True


@pytest.mark.asyncio
async def test_matched_gap_analysis_failure_still_persists_internal_policy_gap(
    monkeypatch,
) -> None:
    from app.services.business_change_policy_workflow_service import (
        process_business_change_policy_impacts,
    )

    change = sample_change()
    captured: list[str] = []

    class _Query:
        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> object:
            return change

    class _Session:
        def query(self, *_args: object, **_kwargs: object) -> _Query:
            return _Query()

    class _Scope:
        def __enter__(self) -> _Session:
            return _Session()

        def __exit__(self, *_args: object) -> None:
            return None

    async def fake_embed(*_args: object, **_kwargs: object) -> list[list[float]]:
        return [[1.0, 0.0]]

    async def fail_gap(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError('gap analysis exploded')

    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.SessionLocal',
        object,
    )
    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.session_scope',
        lambda **_kwargs: _Scope(),
    )
    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.load_internal_policy_match_candidates',
        lambda *_args, **_kwargs: [
            PolicyMatchCandidate(
                policy_id='policy-1',
                policy_name='Supervision Policy',
                policy_version=1,
                excerpt='Supervisors review electronic communications.',
                vectors=((1.0, 0.0),),
            )
        ],
    )
    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.embed_business_change',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.gap_analysis_service.run_gap_analysis',
        fail_gap,
    )
    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.persist_matched_business_change_gap_fallback',
        lambda *_args, **_kwargs: captured.append('fallback') or 'gap-fb',
    )
    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service._mark_matched_business_change_gaps_redline_eligible',
        lambda **_kwargs: None,
    )

    result = await process_business_change_policy_impacts('bc-1', 'org_example')

    assert captured == ['fallback']
    assert result.match.matched is True
    assert result.gap_analysis is not None
    assert result.gap_analysis.run_id == 'gap-fb'
    assert result.new_gap_analysis_id == 'gap-fb'
