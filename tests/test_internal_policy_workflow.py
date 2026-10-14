from unittest.mock import AsyncMock

import pytest

from app.services.internal_policy_workflow_service import (
    InternalPolicyWorkflowResult,
    process_all_internal_policies_for_org,
    process_internal_policy,
)
from app.services.regulatory_reference_policy_service import ALL_REFERENCE_SOURCES


@pytest.mark.asyncio
async def test_process_internal_policy_runs_embed_cosine_and_gap(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_embed(*_args: object, **_kwargs: object) -> int:
        calls.append('embed')
        return 3

    def fake_compare(*_args: object, **_kwargs: object) -> list[object]:
        calls.append('cosine')
        return []

    async def fake_gap(*_args: object, **_kwargs: object) -> object:
        calls.append('gap')
        from app.schemas.agent import AgentRunResponse

        return AgentRunResponse(run_id='run-1', status='completed', message='ok')

    async def fake_redlines(*_args: object, **_kwargs: object) -> int:
        calls.append('redline')
        return 2

    class FakePolicy:
        name = 'FINRA'

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service._load_active_internal_policy',
        lambda *_args, **_kwargs: FakePolicy(),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.embed_internal_policy',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.compare_internal_policy_to_government_reference',
        fake_compare,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.run_gap_analysis_for_policy',
        fake_gap,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.latest_gap_analysis_ids_for_policy',
        lambda *_args, **_kwargs: ['gap-1'],
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.generate_redlines_for_material_gaps',
        fake_redlines,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        lambda *args, **kwargs: _fake_session_scope(),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_regulator_websites',
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        AsyncMock(return_value=1),
    )

    result = await process_internal_policy(
        org_id='org_example',
        policy_id='policy-finra',
        scrape_associated_regulator=False,
    )

    assert isinstance(result, InternalPolicyWorkflowResult)
    assert result.embedded_chunks == 3
    assert result.redlines_generated == 0
    assert calls == ['embed', 'cosine', 'gap', 'cosine']
    assert 'redline' not in calls


@pytest.mark.asyncio
async def test_generate_redlines_for_material_gaps_is_noop() -> None:
    from app.services.internal_policy_workflow_service import generate_redlines_for_material_gaps

    generated = await generate_redlines_for_material_gaps(
        object(),  # type: ignore[arg-type]
        org_id='org_example',
        policy_id='policy-1',
        gap_analysis_ids=['gap-1', 'gap-2'],
    )
    assert generated == 0


@pytest.mark.asyncio
async def test_process_all_internal_policies_for_org_scrapes_all_regulators_once(monkeypatch) -> None:
    order: list[str] = []
    process_calls: list[dict[str, object]] = []
    scrape_calls: list[object] = []

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        scrape_calls.append('refresh')
        return 3

    async def fake_batch_scrape(*_args: object, **_kwargs: object) -> int:
        scrape_calls.append('batch_scrape')
        return 3

    async def fake_process(
        *,
        org_id: str,
        policy_id: str,
        settings: object | None = None,
        scrape_associated_regulator: bool = True,
        scraped_sources: set[str] | None = None,
    ) -> InternalPolicyWorkflowResult:
        del org_id, settings
        order.append(policy_id)
        scrape_calls.append(f'process:{policy_id}')
        process_calls.append(
            {
                'policy_id': policy_id,
                'scrape_associated_regulator': scrape_associated_regulator,
                'scraped_sources': set(scraped_sources or ()),
            }
        )
        return InternalPolicyWorkflowResult(
            policy_id=policy_id,
            embedded_chunks=1,
        )

    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def order_by(self, *_args: object, **_kwargs: object):
            return self

        def all(self) -> list[tuple[str]]:
            return [('policy-finra',), ('policy-sec',)]

    class FakeSession:
        def query(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return FakeQuery()

    def fake_session_scope(*_args: object, **_kwargs: object):
        class _Ctx:
            def __enter__(self):
                return FakeSession()

            def __exit__(self, *_exc: object) -> None:
                return None

        return _Ctx()

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_batch_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.process_internal_policy',
        fake_process,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        fake_session_scope,
    )

    result = await process_all_internal_policies_for_org('org_example')

    assert scrape_calls.count('batch_scrape') == 1
    assert scrape_calls.count('refresh') == 1
    assert scrape_calls.count('process:policy-finra') == 1
    assert scrape_calls.count('process:policy-sec') == 1
    assert scrape_calls.index('refresh') < scrape_calls.index('process:policy-finra')
    assert order == ['policy-finra', 'policy-sec']
    assert all(call['scrape_associated_regulator'] is False for call in process_calls)
    assert result['policies_processed'] == 2
    assert result['reference_policies_updated'] == 3
    assert result['scraped_sources'] == sorted(ALL_REFERENCE_SOURCES)


@pytest.mark.asyncio
async def test_process_all_internal_policies_always_processes_each_policy(monkeypatch) -> None:
    """Cron/upload batch always runs gap analysis for every target policy."""
    process_calls: list[str] = []

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        return 0

    async def fake_batch_scrape(*_args: object, **_kwargs: object) -> int:
        return 0

    async def fake_process(
        *,
        org_id: str,
        policy_id: str,
        settings: object | None = None,
        scrape_associated_regulator: bool = True,
        scraped_sources: set[str] | None = None,
    ) -> InternalPolicyWorkflowResult:
        del org_id, settings, scrape_associated_regulator, scraped_sources
        process_calls.append(policy_id)
        return InternalPolicyWorkflowResult(policy_id=policy_id, embedded_chunks=1)

    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def order_by(self, *_args: object, **_kwargs: object):
            return self

        def all(self) -> list[tuple[str]]:
            return [('policy-a',), ('policy-b',)]

    class FakeSession:
        def query(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return FakeQuery()

    def fake_session_scope(*_args: object, **_kwargs: object):
        class _Ctx:
            def __enter__(self):
                return FakeSession()

            def __exit__(self, *_exc: object) -> None:
                return None

        return _Ctx()

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_batch_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.process_internal_policy',
        fake_process,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        fake_session_scope,
    )

    await process_all_internal_policies_for_org('org_example')

    assert process_calls == ['policy-a', 'policy-b']


@pytest.mark.asyncio
async def test_process_all_internal_policies_for_uploaded_policy_scrapes_all_regulators(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        captured['refresh'] = True
        return 0

    async def fake_batch_scrape(*_args: object, **_kwargs: object) -> int:
        captured['batch_scrape'] = True
        return 3

    async def fake_process(
        *,
        org_id: str,
        policy_id: str,
        settings: object | None = None,
        scrape_associated_regulator: bool = True,
        scraped_sources: set[str] | None = None,
    ) -> InternalPolicyWorkflowResult:
        del org_id, settings, scraped_sources
        captured['policy_id'] = policy_id
        captured['scrape_associated_regulator'] = scrape_associated_regulator
        return InternalPolicyWorkflowResult(
            policy_id=policy_id,
            embedded_chunks=1,
        )

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_batch_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.process_internal_policy',
        fake_process,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        lambda *args, **kwargs: _fake_session_scope(),
    )

    result = await process_all_internal_policies_for_org(
        'org_example',
        policy_ids=['policy-sec'],
    )

    assert captured.get('refresh') is True
    assert captured['batch_scrape'] is True
    assert captured['policy_id'] == 'policy-sec'
    assert captured['scrape_associated_regulator'] is False
    assert result['scraped_sources'] == sorted(ALL_REFERENCE_SOURCES)
    assert result['policies_processed'] == 1
    assert result['reference_policies_updated'] == 3


@pytest.mark.asyncio
async def test_process_internal_policy_live_scrapes_before_analysis(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        calls.append('refresh')
        return 1

    async def fake_batch_scrape(*_args: object, **_kwargs: object) -> int:
        calls.append('scrape_all')
        return 3

    async def fake_embed(*_args: object, **_kwargs: object) -> int:
        calls.append('embed')
        return 2

    def fake_compare(*_args: object, **_kwargs: object) -> list[object]:
        calls.append('cosine')
        return []

    async def fake_gap(*_args: object, **_kwargs: object) -> object:
        calls.append('gap')
        from app.schemas.agent import AgentRunResponse

        return AgentRunResponse(run_id='run-1', status='completed', message='ok')

    class FakePolicy:
        name = 'FINRA'

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service._load_active_internal_policy',
        lambda *_args, **_kwargs: FakePolicy(),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_batch_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.embed_internal_policy',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.compare_internal_policy_to_government_reference',
        fake_compare,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.run_gap_analysis_for_policy',
        fake_gap,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.latest_gap_analysis_ids_for_policy',
        lambda *_args, **_kwargs: ['gap-1'],
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        lambda *args, **kwargs: _fake_session_scope(),
    )

    result = await process_internal_policy(org_id='org_example', policy_id='policy-finra')

    assert 'refresh' in calls
    assert 'scrape_all' in calls
    assert 'embed' in calls
    assert 'gap' in calls
    assert calls.index('refresh') < calls.index('embed')
    assert calls.index('embed') < calls.index('gap')
    assert result.scraped_regulator == ','.join(ALL_REFERENCE_SOURCES)
    assert result.reference_policies_updated == 3


@pytest.mark.asyncio
async def test_process_internal_policy_persists_gap_when_live_scrape_fails(monkeypatch) -> None:
    calls: list[str] = []

    async def fake_refresh(*_args: object, **_kwargs: object) -> int:
        calls.append('refresh')
        return 1

    async def fake_batch_scrape(*_args: object, **_kwargs: object) -> int:
        calls.append('scrape_all')
        raise RuntimeError('SEC 503')

    async def fake_embed(*_args: object, **_kwargs: object) -> int:
        calls.append('embed')
        return 2

    def fake_compare(*_args: object, **_kwargs: object) -> list[object]:
        calls.append('cosine')
        return []

    async def fake_gap(*_args: object, **_kwargs: object) -> object:
        calls.append('gap')
        from app.schemas.agent import AgentRunResponse

        return AgentRunResponse(run_id='run-1', status='completed', message='ok')

    class FakePolicy:
        name = 'AML Policy'

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service._load_active_internal_policy',
        lambda *_args, **_kwargs: FakePolicy(),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_batch_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.embed_internal_policy',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.compare_internal_policy_to_government_reference',
        fake_compare,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.run_gap_analysis_for_policy',
        fake_gap,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.latest_gap_analysis_ids_for_policy',
        lambda *_args, **_kwargs: ['gap-1'],
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        lambda *args, **kwargs: _fake_session_scope(),
    )

    result = await process_internal_policy(org_id='org_new', policy_id='policy-aml')

    assert 'refresh' in calls
    assert 'scrape_all' in calls
    assert 'embed' in calls
    assert 'gap' in calls
    assert calls.index('refresh') < calls.index('embed')
    assert result.gap_analysis is not None
    assert result.gap_analysis_ids == ['gap-1']
    assert 'regulator_scrape_failed' in result.errors


@pytest.mark.asyncio
async def test_process_internal_policy_always_runs_gap_analysis(monkeypatch) -> None:
    """Gap analysis must always run and overwrite — never skip when a gap already exists."""
    calls: list[str] = []

    async def fake_embed(*_args: object, **_kwargs: object) -> int:
        calls.append('embed')
        return 2

    def fake_compare(*_args: object, **_kwargs: object) -> list[object]:
        calls.append('cosine')
        return []

    async def fake_gap(*_args: object, **_kwargs: object) -> object:
        calls.append('gap')
        from app.schemas.agent import AgentRunResponse

        return AgentRunResponse(run_id='run-1', status='completed', message='ok')

    class FakePolicy:
        name = 'AML Policy'

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service._load_active_internal_policy',
        lambda *_args, **_kwargs: FakePolicy(),
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.embed_internal_policy',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.compare_internal_policy_to_government_reference',
        fake_compare,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.run_gap_analysis_for_policy',
        fake_gap,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.latest_gap_analysis_ids_for_policy',
        lambda *_args, **_kwargs: ['gap-existing'],
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.session_scope',
        lambda *args, **kwargs: _fake_session_scope(),
    )

    result = await process_internal_policy(
        org_id='org_example',
        policy_id='policy-aml',
        scrape_associated_regulator=False,
    )

    assert calls == ['embed', 'cosine', 'gap', 'cosine']
    assert result.gap_analysis is not None
    assert result.gap_analysis_ids == ['gap-existing']


class _FakeSessionScope:
    def __enter__(self):
        return object()

    def __exit__(self, *_exc: object) -> None:
        return None


def _fake_session_scope(*_args: object, **_kwargs: object) -> _FakeSessionScope:
    return _FakeSessionScope()
