from datetime import UTC, date, datetime
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from app.models.core import BusinessChange, Organization
from app.services.business_change_analysis_service import (
    ANALYSIS_STATUS_GENERATING,
    BusinessChangeSnapshot,
    build_business_change_analysis_docx,
    business_change_trigger_text,
    fallback_business_change_analysis,
    generate_business_change_analysis_document,
    snapshot_from_change,
)
from app.services.policy_comparison_service import RegulatoryReferenceAttachment
from app.services.redline_generation_service import is_docx_bytes


def sample_change() -> BusinessChange:
    return BusinessChange(
        id='change-1',
        org_id='org_example',
        change_type='NEW_PRODUCT',
        description='Launch a managed account product for retail clients.',
        flagged_by='cco-id',
        flagged_at=datetime.now(UTC),
        change_date=date(2026, 6, 29),
        triggers_review=True,
        assessment_rationale='The product affects client services.',
        assessment_mode='LLM',
        event_summary='**Business Change:** Launching a managed account product.',
    )


def sample_snapshot() -> BusinessChangeSnapshot:
    return snapshot_from_change(sample_change())


def sample_attachment() -> RegulatoryReferenceAttachment:
    return RegulatoryReferenceAttachment(
        policy_id='gov-1',
        policy_name='FINRA Media Center Reference',
        regulator_source='FINRA',
        version=1,
        source_url='https://www.finra.org/media-center',
        content='Member firms must maintain supervisory procedures for new products.',
    )


def test_business_change_trigger_text_prefers_event_summary_and_description() -> None:
    text = business_change_trigger_text(sample_snapshot())
    assert 'Launching a managed account product.' in text
    assert 'Launch a managed account product for retail clients.' in text


def test_build_business_change_analysis_docx_includes_regulatory_reference_attachments() -> None:
    analysis = fallback_business_change_analysis(sample_snapshot(), [sample_attachment()])
    output = build_business_change_analysis_docx(
        change=sample_snapshot(),
        organization_name='Example Capital',
        analysis=analysis,
        reference_attachments=[sample_attachment()],
    )

    assert is_docx_bytes(output) is True
    with ZipFile(BytesIO(output)) as archive:
        document_xml = archive.read('word/document.xml').decode()
    assert 'Business Change Regulatory Analysis' in document_xml
    assert 'FINRA Media Center Reference' in document_xml
    assert 'managed account product' in document_xml


def test_build_business_change_analysis_docx_strips_markdown_markers() -> None:
    snapshot = sample_snapshot()
    analysis = fallback_business_change_analysis(snapshot, [sample_attachment()])
    analysis['executive_summary'] = (
        '**Business Change:** Hire an AML attorney. '
        '**Compliance Impact:** Update WSPs and designation records.'
    )
    output = build_business_change_analysis_docx(
        change=snapshot,
        organization_name='Example Capital',
        analysis=analysis,
        reference_attachments=[sample_attachment()],
    )

    with ZipFile(BytesIO(output)) as archive:
        document_xml = archive.read('word/document.xml').decode()
    assert 'Business Change:' in document_xml
    assert 'Compliance Impact:' in document_xml
    assert '**' not in document_xml


@pytest.mark.asyncio
async def test_generate_business_change_analysis_document_persists_file_without_ready_status(
    monkeypatch,
) -> None:
    change = sample_change()
    organization = Organization(
        id='organization-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )
    uploaded: dict[str, bytes] = {}

    class QueryStub:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object):
            return self

        def one_or_none(self) -> object:
            return self.result

    class SessionStub:
        def __init__(self, result: object) -> None:
            self.result = result
            self.commits = 0

        def query(self, model: object) -> QueryStub:
            if model is BusinessChange:
                return QueryStub(change)
            return QueryStub(organization)

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_session_scope(*, commit: bool = True, org_id: str | None = None):
        del org_id
        session = SessionStub(change)

        class _Scope:
            def __enter__(self) -> SessionStub:
                return session

            def __exit__(self, *_args: object) -> None:
                if commit:
                    session.commit()

        return _Scope()

    def fake_load_attachments(*_args: object, **_kwargs: object) -> list[RegulatoryReferenceAttachment]:
        return [sample_attachment()]

    async def fake_draft(*_args: object, **_kwargs: object) -> dict[str, object]:
        return fallback_business_change_analysis(sample_snapshot(), [sample_attachment()])

    def fake_upload(_bucket: str, _path: str, contents: bytes, _content_type: str) -> SimpleNamespace:
        uploaded['contents'] = contents
        return SimpleNamespace(uri='supabase://business-change-analyses/org_example/change-1.docx')

    monkeypatch.setattr(
        'app.services.business_change_analysis_service.SessionLocal',
        object,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.session_scope',
        fake_session_scope,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.load_regulatory_reference_attachments',
        fake_load_attachments,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.draft_business_change_analysis',
        fake_draft,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.upload_file',
        fake_upload,
    )

    response = await generate_business_change_analysis_document('change-1', 'org_example')

    assert response.status == 'completed'
    assert change.analysis_status == ANALYSIS_STATUS_GENERATING
    assert change.analysis_file_url is not None
    assert is_docx_bytes(uploaded['contents']) is True


@pytest.mark.asyncio
async def test_generate_keeps_generating_status_when_llm_draft_times_out(monkeypatch) -> None:
    change = sample_change()
    change.analysis_status = 'GENERATING'

    class QueryStub:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object):
            return self

        def one_or_none(self) -> object:
            return self.result

    class SessionStub:
        def __init__(self, result: object) -> None:
            self.result = result

        def query(self, model: object) -> QueryStub:
            if model is BusinessChange:
                return QueryStub(change)
            return QueryStub(None)

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_session_scope(*, commit: bool = True, org_id: str | None = None):
        del commit, org_id
        session = SessionStub(change)

        class _Scope:
            def __enter__(self) -> SessionStub:
                return session

            def __exit__(self, *_args: object) -> None:
                return None

        return _Scope()

    async def hang_draft(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise TimeoutError('draft timed out')

    monkeypatch.setattr('app.services.business_change_analysis_service.SessionLocal', object)
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.session_scope',
        fake_session_scope,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.load_regulatory_reference_attachments',
        lambda *_args, **_kwargs: [sample_attachment()],
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.draft_business_change_analysis',
        hang_draft,
    )
    monkeypatch.setattr(
        'app.services.business_change_analysis_service.upload_file',
        lambda *_args, **_kwargs: SimpleNamespace(uri='supabase://ready.docx'),
    )

    response = await generate_business_change_analysis_document('change-1', 'org_example')

    assert response.status == 'completed'
    assert change.analysis_status == ANALYSIS_STATUS_GENERATING
    assert change.analysis_file_url == 'supabase://ready.docx'
