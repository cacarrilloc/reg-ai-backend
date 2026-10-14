import pytest

from app.core.config import Settings
from app.schemas.agent import AgentRunRequest
from app.services.workflow_dispatch_service import (
    run_policy_upload_workflow,
    should_run_background_workflow,
    workflow_agent_request,
)


def test_workflow_agent_request_always_reruns_gap_analysis() -> None:
    request = workflow_agent_request(
        trigger_type='business_change',
        trigger_id='change-id',
        org_id='org_example',
    )

    assert request == AgentRunRequest(
        trigger_type='business_change',
        trigger_id='change-id',
        org_id='org_example',
        policy_ids=None,
        force_reanalysis=True,
        auto_generate_redlines=None,
    )


def test_background_workflow_runs_only_when_inngest_did_not_deliver() -> None:
    settings = Settings(ENVIRONMENT='local', INNGEST_EVENT_KEY='test-key', INNGEST_SIGNING_KEY='signing')

    assert should_run_background_workflow(settings, inngest_delivered=True) is False
    assert should_run_background_workflow(settings, inngest_delivered=False) is True


@pytest.mark.asyncio
async def test_run_policy_upload_workflow_scrapes_and_gap_analyzes_uploaded_policy(
    monkeypatch,
) -> None:
    """Upload scrapes FINRA/SEC/CFTC, refreshes regulation embeddings, then gap-analyzes."""
    captured: dict[str, object] = {}

    async def fake_process_all(
        org_id: str,
        *,
        settings: object | None = None,
        policy_ids: list[str] | None = None,
        skip_scrape: bool = False,
        scrape_sources: object | None = None,
    ) -> dict[str, object]:
        del settings, scrape_sources
        captured['org_id'] = org_id
        captured['policy_ids'] = policy_ids
        captured['skip_scrape'] = skip_scrape
        return {
            'org_id': org_id,
            'policies_processed': 1,
            'embedded_chunks': 4,
            'redlines_generated': 0,
            'gap_analysis_runs': 1,
            'scraped_sources': ['FINRA'],
            'reference_policies_updated': 1,
            'errors': [],
        }

    monkeypatch.setattr(
        'app.services.workflow_dispatch_service.process_all_internal_policies_for_org',
        fake_process_all,
    )

    result = await run_policy_upload_workflow('policy-finra', 'org_example')

    assert captured == {
        'org_id': 'org_example',
        'policy_ids': ['policy-finra'],
        'skip_scrape': False,
    }
    assert result['status'] == 'completed'
    assert result['scraped_sources'] == ['FINRA']
    assert result['gap_analysis_runs'] == 1
    assert result['reference_policies_updated'] == 1


@pytest.mark.asyncio
async def test_run_policy_upload_workflow_skips_pipeline_for_upload_pending_clear(
    monkeypatch,
) -> None:
    """Re-upload that clears UPLOAD_PENDING skips scrape, embed, and gap analysis."""
    called = False

    async def fake_process_all(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(
        'app.services.workflow_dispatch_service.process_all_internal_policies_for_org',
        fake_process_all,
    )

    result = await run_policy_upload_workflow(
        'policy-finra-v2',
        'org_example',
        skip_pipeline=True,
    )

    assert called is False
    assert result['status'] == 'completed'
    assert result['policies_analyzed'] == 0
    assert result['scraped_sources'] == []
    assert result['embedded_chunks'] == 0
    assert result['gap_analysis_runs'] == 0
    assert 'UPLOAD_PENDING' in str(result['message'])
