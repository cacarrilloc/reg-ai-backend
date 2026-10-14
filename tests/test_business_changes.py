from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest
from ai_fakes import FakeLLM, result
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.inngest import business_change_flagged_handler
from app.core.security import CurrentUser
from app.main import app
from app.models.core import BusinessChange, BusinessChangeEventDelivery, Organization
from app.schemas.agent import AgentRunResponse
from app.schemas.policies import BusinessChangeCreate
from app.services.business_change_assessment import (
    BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT,
    BusinessChangeAssessment,
    assess_business_change,
    parse_assessment_fields_loosely,
    parse_event_summary_fields_loosely,
    parse_event_summary_response,
)
from app.services.business_change_service import (
    create_business_change,
    emit_pending_business_change_events,
)


def test_create_business_change_without_database_returns_assessment() -> None:
    client = TestClient(app)
    response = client.post(
        '/api/v1/business-changes',
        json={
            'change_type': 'NEW_PRODUCT',
            'description': 'Launching a new advisory product for clients.',
            'change_date': '2026-06-10',
        },
        headers={'x-org-id': 'test-org'},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload['org_id'] == 'test-org'
    assert payload['change_date'] == '2026-06-10'
    assert payload['triggers_review'] is True
    assert payload['assessment_mode'] == 'RULE_FALLBACK'
    assert payload['persisted'] is False
    assert payload['event_emitted'] is False


def test_product_manager_cannot_submit_business_change() -> None:
    client = TestClient(app)
    response = client.post(
        '/api/v1/business-changes',
        json={
            'change_type': 'NEW_PRODUCT',
            'description': 'Launching a new advisory product for clients.',
        },
        headers={'x-org-id': 'test-org', 'x-user-role': 'product_manager'},
    )

    assert response.status_code == 403


def test_parse_event_summary_response_recovers_truncated_json() -> None:
    response = result(
        '{"event_summary": "Launching a managed account product may require '
        'WSP updates for suitability and supervision.'
    )

    payload = parse_event_summary_response(response)

    assert payload.event_summary.startswith('Launching a managed account product')


def test_parse_assessment_fields_loosely_reads_rationale() -> None:
    payload = parse_assessment_fields_loosely(
        '{"triggers_review": true, "rationale": "The product affects client services.",'
    )

    assert payload == {
        'triggers_review': True,
        'rationale': 'The product affects client services.',
    }


def test_parse_event_summary_fields_loosely_reads_summary() -> None:
    payload = parse_event_summary_fields_loosely(
        '{"event_summary": "Centralized onboarding may require supervisory updates."}'
    )

    assert payload == {'event_summary': 'Centralized onboarding may require supervisory updates.'}


def test_parse_event_summary_response_truncates_overlong_summary() -> None:
    from app.services.business_change_assessment import BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS

    long_summary = 'A' * (BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS + 500)
    response = result(json.dumps({'event_summary': long_summary}))

    payload = parse_event_summary_response(response)

    assert len(payload.event_summary) == BUSINESS_CHANGE_EVENT_SUMMARY_MAX_CHARS
    assert payload.event_summary.endswith('...')


@pytest.mark.asyncio
async def test_opus_assessment_uses_json_contract_and_firm_scope() -> None:
    def respond(_messages: object, kwargs: dict[str, object]) -> str:
        if kwargs.get('system') == BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT:
            return '{"triggers_review": true, "rationale": "The product affects client services."}'
        return '{"event_summary": "Launching a managed account product may require WSP updates."}'

    client = FakeLLM(model_label='claude-opus-4-6', responder=respond)
    settings = Settings(ANTHROPIC_API_KEY='test-key')

    assessment = await assess_business_change(
        change_type='NEW_PRODUCT',
        description='Launch a managed account product for retail clients.',
        change_date=None,
        regulatory_scope={'sources': ['FINRA'], 'jurisdiction': 'US'},
        settings=settings,
        client=client,
    )

    assert assessment == BusinessChangeAssessment(
        triggers_review=True,
        rationale='The product affects client services.',
        mode='LLM',
        model='claude-opus-4-6',
        event_summary='Launching a managed account product may require WSP updates.',
    )
    assessment_request = client.calls[0]
    assert 'temperature' not in assessment_request
    assert assessment_request['system'] == BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT
    assert '"sources": ["FINRA"]' in assessment_request['messages'][0].content


@pytest.mark.asyncio
async def test_unavailable_opus_fails_safe_to_required_review() -> None:
    assessment = await assess_business_change(
        change_type='OTHER',
        description='An operational change that needs classification.',
        change_date=None,
        regulatory_scope={},
        settings=Settings(ANTHROPIC_API_KEY='test-key'),
        client=FakeLLM(RuntimeError('provider unavailable')),
    )

    assert assessment.triggers_review is True
    assert assessment.mode == 'FAIL_SAFE'


@pytest.mark.asyncio
async def test_flagged_change_persists_assessment_and_queues_background_workflow(monkeypatch) -> None:
    organization = Organization(
        id='organization-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    class OrganizationQuery:
        def filter(self, *_args: object) -> OrganizationQuery:
            return self

        def one_or_none(self) -> Organization:
            return organization

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.commits = 0

        def query(self, model: object) -> OrganizationQuery:
            assert model is Organization
            return OrganizationQuery()

        def add(self, value: object) -> None:
            self.added.append(value)

        def begin_nested(self):
            return nullcontext()

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

    class RecordingBackgroundTasks:
        def __init__(self) -> None:
            self.tasks: list[tuple[object, tuple[object, ...]]] = []

        def add_task(self, func: object, *args: object) -> None:
            self.tasks.append((func, args))

    async def fake_assessment(**_kwargs: object) -> BusinessChangeAssessment:
        return BusinessChangeAssessment(
            triggers_review=True,
            rationale='The new client process changes supervisory controls.',
            mode='LLM',
            model='claude-opus-4-6',
        )

    monkeypatch.setattr(
        'app.services.business_change_service.assess_business_change', fake_assessment)
    monkeypatch.setattr(
        'app.services.business_change_service.get_settings',
        lambda: Settings(INNGEST_EVENT_KEY='test-key'),
    )
    db = RecordingSession()
    background_tasks = RecordingBackgroundTasks()
    response = await create_business_change(
        BusinessChangeCreate(
            change_type='NEW_PROCESS',
            description='Move client onboarding supervision to a centralized operations team.',
        ),
        CurrentUser(user_id='cco-id', org_id='org_example',
                    role='chief_compliance_officer'),
        db,  # type: ignore[arg-type]
        background_tasks,  # type: ignore[arg-type]
    )

    changes = [value for value in db.added if isinstance(
        value, BusinessChange)]
    deliveries = [value for value in db.added if isinstance(
        value, BusinessChangeEventDelivery)]
    assert len(changes) == 1
    assert changes[0].assessment_mode == 'LLM'
    assert changes[0].assessment_rationale == 'The new client process changes supervisory controls.'
    assert len(deliveries) == 0
    assert len(background_tasks.tasks) == 1
    assert response.persisted is True
    assert response.event_emitted is True


@pytest.mark.asyncio
async def test_flagged_change_persists_inngest_delivery_in_production(monkeypatch) -> None:
    organization = Organization(
        id='organization-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    class OrganizationQuery:
        def filter(self, *_args: object) -> OrganizationQuery:
            return self

        def one_or_none(self) -> Organization:
            return organization

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.commits = 0

        def query(self, model: object) -> OrganizationQuery:
            assert model is Organization
            return OrganizationQuery()

        def add(self, value: object) -> None:
            self.added.append(value)

        def begin_nested(self):
            return nullcontext()

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

    async def fake_assessment(**_kwargs: object) -> BusinessChangeAssessment:
        return BusinessChangeAssessment(
            triggers_review=True,
            rationale='The new client process changes supervisory controls.',
            mode='LLM',
            model='claude-opus-4-6',
        )

    async def fake_emit(*_args: object, **_kwargs: object) -> int:
        return 1

    monkeypatch.setattr(
        'app.services.business_change_service.assess_business_change', fake_assessment)
    monkeypatch.setattr(
        'app.services.business_change_service.emit_pending_business_change_events', fake_emit)
    monkeypatch.setattr(
        'app.services.business_change_service.get_settings',
        lambda: Settings(
            ENVIRONMENT='production',
            INNGEST_EVENT_KEY='test-key',
            INNGEST_SIGNING_KEY='signing-key',
        ),
    )
    db = RecordingSession()
    response = await create_business_change(
        BusinessChangeCreate(
            change_type='NEW_PROCESS',
            description='Move client onboarding supervision to a centralized operations team.',
        ),
        CurrentUser(user_id='cco-id', org_id='org_example',
                    role='chief_compliance_officer'),
        db,  # type: ignore[arg-type]
    )

    deliveries = [value for value in db.added if isinstance(
        value, BusinessChangeEventDelivery)]
    assert len(deliveries) == 1
    assert response.persisted is True
    assert response.event_emitted is True


@pytest.mark.asyncio
async def test_business_change_outbox_emits_flagged_event_and_marks_delivery_sent() -> None:
    change = BusinessChange(
        id='change-id',
        org_id='org_example',
        change_type='NEW_HIRE',
        description='Hire a new operations supervisor.',
        flagged_by='cco-id',
        triggers_review=True,
    )
    delivery = BusinessChangeEventDelivery(
        id='delivery-id', business_change_id=change.id, org_id='org_example'
    )

    class Query:
        def join(self, *_args: object) -> Query:
            return self

        def filter(self, *_args: object) -> Query:
            return self

        def order_by(self, *_args: object) -> Query:
            return self

        def limit(self, *_args: object) -> Query:
            return self

        def all(self) -> list[tuple[BusinessChangeEventDelivery, BusinessChange]]:
            return [(delivery, change)]

    class RecordingSession:
        def __init__(self) -> None:
            self.committed = False

        def query(self, *_args: object) -> Query:
            return Query()

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            return None

    class FakeInngestClient:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def send(self, events: list[Any]) -> list[str]:
            self.events = events
            return ['event-id']

    db = RecordingSession()
    client = FakeInngestClient()
    emitted = await emit_pending_business_change_events(
        db,  # type: ignore[arg-type]
        Settings(
            ENVIRONMENT='production',
            INNGEST_EVENT_KEY='test-event-key',
            INNGEST_SIGNING_KEY='test-signing-key',
        ),
        client=client,
    )

    assert emitted == 1
    assert client.events[0].name == 'business_change.flagged'
    assert client.events[0].data['business_change_id'] == 'change-id'
    assert client.events[0].data['org_id'] == 'org_example'
    assert delivery.event_id == 'event-id'
    assert delivery.sent_at is not None
    assert db.committed is True


@pytest.mark.asyncio
async def test_business_change_trigger_path_reaches_shared_agent_workflow(monkeypatch) -> None:
    organization = Organization(
        id='organization-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    class OrganizationQuery:
        def filter(self, *_args: object) -> OrganizationQuery:
            return self

        def one_or_none(self) -> Organization:
            return organization

    class DeliveryQuery:
        def __init__(self, db: RecordingSession) -> None:
            self.db = db

        def join(self, *_args: object) -> DeliveryQuery:
            return self

        def filter(self, *_args: object) -> DeliveryQuery:
            return self

        def order_by(self, *_args: object) -> DeliveryQuery:
            return self

        def limit(self, *_args: object) -> DeliveryQuery:
            return self

        def all(self) -> list[tuple[BusinessChangeEventDelivery, BusinessChange]]:
            changes = [item for item in self.db.added if isinstance(
                item, BusinessChange)]
            deliveries = [item for item in self.db.added if isinstance(
                item, BusinessChangeEventDelivery)]
            return [(deliveries[0], changes[0])]

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, *models: object) -> OrganizationQuery | DeliveryQuery:
            if models == (Organization,):
                return OrganizationQuery()
            assert models == (BusinessChangeEventDelivery, BusinessChange)
            return DeliveryQuery(self)

        def add(self, value: object) -> None:
            self.added.append(value)

        def begin_nested(self):
            return nullcontext()

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class FakeInngestClient:
        events: list[Any] = []

        def __init__(self, **_kwargs: object) -> None:
            return None

        async def send(self, events: list[Any]) -> list[str]:
            self.__class__.events = events
            return ['event-id']

    captured: dict[str, str | None] = {}

    async def fake_workflow(change_id: str, org_id: str) -> AgentRunResponse:
        captured['trigger_type'] = 'business_change'
        captured['trigger_id'] = change_id
        captured['org_id'] = org_id
        return AgentRunResponse(run_id='run-id', status='queued', message='Queued')

    settings = Settings(
        ENVIRONMENT='production',
        INNGEST_EVENT_KEY='test-event-key',
        INNGEST_SIGNING_KEY='test-signing-key',
    )
    monkeypatch.setattr(
        'app.services.business_change_service.get_settings', lambda: settings)
    monkeypatch.setattr(
        'app.core.inngest.run_business_change_workflow', fake_workflow)
    monkeypatch.setattr('inngest.Inngest', FakeInngestClient)
    db = RecordingSession()

    created = await create_business_change(
        BusinessChangeCreate(
            change_type='NEW_PROCESS',
            description='Centralize operational oversight for new client account onboarding.',
        ),
        CurrentUser(user_id='cco-id', org_id='org_example',
                    role='chief_compliance_officer'),
        db,  # type: ignore[arg-type]
    )
    event = FakeInngestClient.events[0]
    workflow_response = await business_change_flagged_handler(
        SimpleNamespace(event=SimpleNamespace(data=event.data))
    )

    assert created.persisted is True
    assert created.triggers_review is True
    assert created.event_emitted is True
    assert event.name == 'business_change.flagged'
    assert captured == {
        'trigger_type': 'business_change',
        'trigger_id': created.id,
        'org_id': 'org_example',
    }
    assert workflow_response['status'] == 'queued'


@pytest.mark.asyncio
async def test_flagged_event_starts_business_change_workflow(monkeypatch) -> None:
    captured: dict[str, str | None] = {}

    async def fake_workflow(change_id: str, org_id: str) -> AgentRunResponse:
        captured['change_id'] = change_id
        captured['org_id'] = org_id
        return AgentRunResponse(run_id='run-id', status='queued', message='Queued')

    monkeypatch.setattr(
        'app.core.inngest.run_business_change_workflow', fake_workflow)
    context = SimpleNamespace(
        event=SimpleNamespace(
            data={'business_change_id': 'change-id', 'org_id': 'org_example'})
    )

    response = await business_change_flagged_handler(context)

    assert captured == {
        'change_id': 'change-id',
        'org_id': 'org_example',
    }
    assert response['status'] == 'queued'


@pytest.mark.asyncio
async def test_business_change_workflow_marks_ready_only_after_policy_matching(
    monkeypatch,
) -> None:
    from app.services import workflow_dispatch_service
    from app.services.business_change_policy_workflow_service import (
        BusinessChangePolicyMatchResult,
        BusinessChangePolicyWorkflowResult,
    )

    captured: dict[str, object] = {}
    order: list[str] = []

    monkeypatch.setattr(
        'app.services.business_change_analysis_service.mark_business_change_analysis_status',
        lambda change_id, org_id, status: order.append(f'status:{status}'),
    )

    async def fake_analysis(change_id: str, org_id: str):
        captured['analysis'] = (change_id, org_id)
        from app.db.session import get_tenant_org_id

        captured['tenant'] = get_tenant_org_id()
        order.append('analysis')
        return AgentRunResponse(run_id=change_id, status='completed', message='Analysis ready')

    async def fake_policy(*_args: object, **_kwargs: object):
        order.append('policy')
        return BusinessChangePolicyWorkflowResult(
            match=BusinessChangePolicyMatchResult(
                matched=True,
                matched_policy_ids=['policy-1'],
                rationale='Matched supervision policy.',
                mode='COSINE',
            ),
            gap_analysis=AgentRunResponse(
                run_id='gap-1',
                status='completed',
                message='Gap persisted',
            ),
            new_policy_id=None,
            new_gap_analysis_id=None,
        )

    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.process_business_change_policy_impacts',
        fake_policy,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.generate_business_change_analysis_document',
        fake_analysis,
    )

    response = await workflow_dispatch_service.run_business_change_workflow('change-1', 'org_example')

    assert captured['analysis'] == ('change-1', 'org_example')
    assert captured['tenant'] == 'org_example'
    assert order == ['policy', 'analysis', 'status:READY']
    assert response.status == 'completed'


@pytest.mark.asyncio
async def test_business_change_workflow_marks_failed_when_policy_matching_raises(
    monkeypatch,
) -> None:
    from app.services import workflow_dispatch_service

    order: list[str] = []

    monkeypatch.setattr(
        'app.services.business_change_analysis_service.mark_business_change_analysis_status',
        lambda change_id, org_id, status: order.append(status),
    )

    async def fail_policy(*_args: object, **_kwargs: object):
        raise RuntimeError('policy matching exploded')

    async def fake_analysis(change_id: str, org_id: str):
        from app.db.session import get_tenant_org_id

        assert get_tenant_org_id() == org_id
        return AgentRunResponse(run_id=change_id, status='completed', message='Analysis ready')

    monkeypatch.setattr(
        'app.services.business_change_policy_workflow_service.process_business_change_policy_impacts',
        fail_policy,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.generate_business_change_analysis_document',
        fake_analysis,
    )

    with pytest.raises(RuntimeError, match='policy matching exploded'):
        await workflow_dispatch_service.run_business_change_workflow('change-1', 'org_example')

    assert order == ['FAILED']


@pytest.mark.asyncio
async def test_flagged_event_without_required_data_is_ignored() -> None:
    response = await business_change_flagged_handler(SimpleNamespace(event=SimpleNamespace(data={})))

    assert response == {
        'status': 'ignored',
        'message': 'business_change.flagged requires business_change_id and org_id',
    }


def test_delete_business_change_removes_change_and_related_gaps() -> None:
    from app.models.core import GapAnalysis
    from app.services.business_change_service import delete_business_change_for_org

    change = BusinessChange(
        id='change-1',
        org_id='org_example',
        change_type='NEW_PROCESS',
        description='Centralize onboarding supervision.',
    )
    gap = GapAnalysis(
        id='gap-1',
        org_id='org_example',
        business_change_id='change-1',
        trigger_type='business_change',
        trigger_id='change-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Needs a policy update.',
    )
    deleted: list[object] = []

    class _Query:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> object | None:
            return self.rows[0] if self.rows else None

        def all(self) -> list[object]:
            return self.rows

        def delete(self, synchronize_session: bool = False) -> int:
            del synchronize_session
            return 0

    class _Session:
        def query(self, model: object) -> _Query:
            if model is BusinessChange:
                return _Query([change])
            if model is GapAnalysis:
                return _Query([gap])
            return _Query([])

        def delete(self, value: object) -> None:
            deleted.append(value)

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    assert delete_business_change_for_org(_Session(), 'org_example', 'change-1') is True
    assert change in deleted
    assert gap in deleted


def test_trigger_requeues_analysis_when_already_generating(monkeypatch) -> None:
    from app.services.business_change_service import trigger_business_change_analysis_generation

    marked: list[str] = []
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.mark_business_change_analysis_status',
        lambda change_id, org_id, status: marked.append(status),
    )

    change = BusinessChange(
        id='change-1',
        org_id='org_example',
        change_type='NEW_HIRE',
        description='Hire an AMLCO in Singapore.',
        triggers_review=True,
        assessment_rationale='The hire touches AML supervision.',
        assessment_mode='LLM',
        analysis_status='GENERATING',
    )
    queued: list[tuple[object, tuple[object, ...]]] = []

    class _Query:
        def __init__(self, one: object | None = None, rows: list[object] | None = None) -> None:
            self.one = one
            self.rows = rows or []

        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def one_or_none(self) -> object:
            return self.one

        def all(self) -> list[object]:
            return self.rows

    class _Session:
        def query(self, model: object, *_args: object, **_kwargs: object) -> _Query:
            if model is BusinessChange:
                return _Query(one=change)
            return _Query()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    class _Background:
        def add_task(self, task: object, *args: object) -> None:
            queued.append((task, args))

    result = trigger_business_change_analysis_generation(
        _Session(),
        'org_example',
        'change-1',
        _Background(),
    )

    assert result['analysis_status'] == 'GENERATING'
    assert result['message'] == 'Business change regulatory analysis generation queued.'
    assert marked == ['GENERATING']
    assert len(queued) == 1
    assert queued[0][1] == ('change-1', 'org_example')


@pytest.mark.asyncio
async def test_replay_queues_background_instead_of_awaiting_workflow(monkeypatch) -> None:
    from app.services import workflow_dispatch_service

    change = BusinessChange(
        id='change-1',
        org_id='org_example',
        change_type='NEW_HIRE',
        description='Hire an AMLCO in Singapore.',
        analysis_status='GENERATING',
    )
    queued: list[tuple[object, tuple[object, ...]]] = []

    class _Query:
        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def limit(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def all(self) -> list[BusinessChange]:
            return [change]

    class _Session:
        def query(self, *_args: object, **_kwargs: object) -> _Query:
            return _Query()

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    class _Scope:
        def __enter__(self) -> _Session:
            return _Session()

        def __exit__(self, *_args: object) -> None:
            return None

    class _Background:
        def add_task(self, task: object, *args: object) -> None:
            queued.append((task, args))

    async def hang_workflow(*_args: object, **_kwargs: object) -> AgentRunResponse:
        raise AssertionError('replay must not await the workflow')

    monkeypatch.setattr('app.db.session.SessionLocal', object)
    monkeypatch.setattr('app.db.session.session_scope', lambda **_kwargs: _Scope())
    monkeypatch.setattr(
        workflow_dispatch_service,
        'run_business_change_workflow',
        hang_workflow,
    )

    responses = await workflow_dispatch_service.replay_business_change_workflows(
        'org_example',
        background_tasks=_Background(),
    )

    assert [response.status for response in responses] == ['queued']
    assert len(queued) == 1
    assert queued[0][1] == ('change-1', 'org_example')


def test_list_business_changes_does_not_write_analysis_documents(monkeypatch) -> None:
    from app.services.business_change_service import list_business_changes_for_org

    persist_calls: list[object] = []
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.persist_ready_business_change_analysis_document',
        lambda *args, **kwargs: persist_calls.append((args, kwargs)),
    )

    change = BusinessChange(
        id='change-1',
        org_id='org_example',
        change_type='NEW_HIRE',
        description='Hire an AMLCO in Singapore.',
        triggers_review=True,
        assessment_rationale='The hire touches AML supervision.',
        assessment_mode='LLM',
        analysis_status='GENERATING',
    )

    class _Query:
        def __init__(self, rows: list[object]) -> None:
            self.rows = rows

        def filter(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def limit(self, *_args: object, **_kwargs: object) -> _Query:
            return self

        def all(self) -> list[object]:
            return self.rows

    class _Session:
        def query(self, model: object) -> _Query:
            if model is BusinessChange:
                return _Query([change])
            return _Query([])

        def commit(self) -> None:
            raise AssertionError('listing business changes must stay read-only')

        def rollback(self) -> None:
            return None

    items = list_business_changes_for_org(_Session(), 'org_example')

    assert len(items) == 1
    assert items[0].id == 'change-1'
    assert persist_calls == []
