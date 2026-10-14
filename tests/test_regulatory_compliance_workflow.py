from contextlib import contextmanager
from unittest.mock import AsyncMock

import pytest

from app.services.regulatory_compliance_workflow_service import (
    run_part2_internal_policy_gap_review,
    run_regulatory_compliance_workflow,
)
from app.workers.jobs import run_regulatory_compliance_workflow_job


@contextmanager
def fake_session_scope(*, commit: bool = True, org_id: str | None = None):
    del commit, org_id
    yield FakeSession()


@contextmanager
def fake_platform_session_scope(*, commit: bool = True):
    del commit
    yield FakeSession()


class FakeSession:
    def query(self, *_args: object, **_kwargs: object) -> object:
        return self

    def filter(self, *_args: object, **_kwargs: object) -> object:
        return self

    def distinct(self) -> object:
        return self

    def order_by(self, *_args: object, **_kwargs: object) -> object:
        return self

    def count(self) -> int:
        return 1

    def all(self) -> list[tuple[str]]:
        return [('org_example',)]

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_schedule_regulatory_compliance_workflow_queues_background_task(monkeypatch) -> None:
    created: list[str] = []

    def fake_create_task(coro, *, name=None):
        created.append(name or 'unnamed')
        coro.close()
        return AsyncMock()

    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.asyncio.create_task',
        fake_create_task,
    )

    from app.services.regulatory_compliance_workflow_service import (
        schedule_regulatory_compliance_workflow,
    )

    schedule_regulatory_compliance_workflow('org_example')

    assert created == ['regulatory-compliance-org_example']


@pytest.mark.asyncio
async def test_run_regulatory_compliance_workflow_scrapes_once_then_loops_orgs(
    monkeypatch,
) -> None:
    org_result = {
        'policies_processed': 2,
        'gap_analysis_runs': 2,
        'redlines_generated': 0,
        'reference_policies_updated': 0,
        'scraped_sources': [],
    }
    scrape_calls: list[str] = []
    refresh_calls: list[str] = []

    async def fake_org_scrape(org_id: str, settings=None):
        del settings
        scrape_calls.append(org_id)
        return {
            'status': 'completed',
            'scraped_sources': ['FINRA', 'SEC', 'CFTC'],
            'fetched_count': 3,
            'created_count': 1,
            'errors': [],
        }

    async def fake_refresh(db, *, org_id, settings=None, sources=None):
        del db, settings, sources
        refresh_calls.append(org_id)
        return 2

    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.session_scope',
        fake_session_scope,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.platform_session_scope',
        fake_platform_session_scope,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.list_candidate_org_ids',
        lambda db, org_id=None: ['org_example'],
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.count_active_internal_policies',
        lambda db, org_id=None: 2,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.run_org_regulatory_scrape',
        fake_org_scrape,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.refresh_org_government_reference_embeddings',
        fake_refresh,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.run_part2_internal_policy_gap_review',
        AsyncMock(return_value=org_result),
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.set_tenant_org_id',
        lambda org_id: object(),
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.reset_tenant_org_id',
        lambda token: None,
    )

    result = await run_regulatory_compliance_workflow()

    assert scrape_calls == ['org_example']
    assert refresh_calls == ['org_example']
    assert result['status'] == 'completed'
    assert result['orgs_processed'] == 1
    assert result['policies_analyzed'] == 2
    assert result['gap_analysis_runs'] == 2
    assert result['reference_policies_updated'] == 2
    assert result['scraped_sources'] == ['CFTC', 'FINRA', 'SEC']
    assert result['part1']['status'] == 'completed'


@pytest.mark.asyncio
async def test_run_regulatory_compliance_workflow_skips_without_internal_policies(
    monkeypatch,
) -> None:
    scrape_calls: list[str] = []

    async def fake_org_scrape(org_id: str, settings=None):
        del settings
        scrape_calls.append(org_id)
        return {'status': 'completed', 'scraped_sources': ['SEC']}

    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.session_scope',
        fake_session_scope,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.platform_session_scope',
        fake_platform_session_scope,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.list_candidate_org_ids',
        lambda db, org_id=None: ['org_example'],
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.count_active_internal_policies',
        lambda db, org_id=None: 0,
    )
    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.run_org_regulatory_scrape',
        fake_org_scrape,
    )

    result = await run_regulatory_compliance_workflow()

    assert scrape_calls == []
    assert result['status'] == 'skipped'
    assert result['reason'] == 'no_internal_policies'
    assert result['orgs_processed'] == 0
    assert result['scraped_sources'] == []


@pytest.mark.asyncio
async def test_run_part2_internal_policy_gap_review_skips_scrape_by_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    async def fake_process_all_internal_policies_for_org(
        org_id: str,
        *,
        settings=None,
        policy_ids=None,
        skip_scrape=False,
    ):
        del settings, policy_ids
        captured['org_id'] = org_id
        captured['skip_scrape'] = skip_scrape
        return {
            'policies_processed': 1,
            'gap_analysis_runs': 1,
            'redlines_generated': 0,
            'scraped_sources': [],
        }

    monkeypatch.setattr(
        'app.services.regulatory_compliance_workflow_service.process_all_internal_policies_for_org',
        fake_process_all_internal_policies_for_org,
    )

    result = await run_part2_internal_policy_gap_review(
        'org_example',
        reference_policies_updated=0,
    )

    assert captured['org_id'] == 'org_example'
    assert captured['skip_scrape'] is True
    assert result['policies_processed'] == 1
    assert result['gap_analysis_runs'] == 1


@pytest.mark.asyncio
async def test_run_regulatory_compliance_workflow_job_awaits_full_workflow(monkeypatch) -> None:
    called: list[str | None] = []

    async def fake_workflow(*, org_id=None):
        called.append(org_id)
        return {
            'status': 'completed',
            'orgs_processed': 1,
            'policies_analyzed': 1,
        }

    monkeypatch.setattr(
        'app.workers.jobs.run_regulatory_compliance_workflow',
        fake_workflow,
    )

    result = await run_regulatory_compliance_workflow_job()

    assert called == [None]
    assert result['status'] == 'completed'
    assert result['org_id'] is None
