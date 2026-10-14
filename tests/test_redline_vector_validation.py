from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.models.core import DocumentChunk
from app.services.redline_generation_service import finalize_policy_redline_for_revision
from app.services.redline_vector_validation_service import (
    NO_RELATED_REDLINE_CONTENT_MESSAGE,
    RedlineVectorValidationResult,
    cosine_similarity,
    mean_max_cosine_similarity,
    validate_redline_against_internal_policy,
)


@pytest.fixture(autouse=True)
def mock_redline_regulator_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_scrape(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.scrape_and_refresh_all_regulator_references',
        fake_scrape,
    )
    monkeypatch.setattr(
        'app.services.internal_policy_workflow_service.refresh_org_government_reference_embeddings',
        fake_scrape,
    )


@pytest.fixture(autouse=True)
def mock_redline_policy_vector_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_load_policy_vectors(*_args: object, **_kwargs: object) -> list[list[float]]:
        return [[1.0, 0.0, 0.0]]

    monkeypatch.setattr(
        "app.services.redline_generation_service.load_policy_vectors_for_validation",
        fake_load_policy_vectors,
    )


@pytest.fixture(autouse=True)
def mock_redline_vector_comparisons(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_persist_and_compare(*_args: object, **_kwargs: object) -> tuple[list[object], list[object]]:
        return [], []

    monkeypatch.setattr(
        "app.services.redline_generation_service._persist_and_compare_redline_vectors",
        fake_persist_and_compare,
    )


@pytest.fixture(autouse=True)
def mock_redline_revision_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime

    from app.models.core import PolicyRevision

    def fake_persist(
        revision_id: str,
        org_id: str,
        redline_file_url: str,
        db: object | None = None,
        **_: object,
    ) -> PolicyRevision:
        return PolicyRevision(
            id=revision_id,
            gap_analysis_id="gap-id",
            policy_id="policy-id",
            org_id=org_id,
            redline_file_url=redline_file_url,
            status="PENDING",
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(
        "app.services.redline_generation_service.persist_revision_redline_url",
        fake_persist,
    )


def test_cosine_similarity_identical_vectors() -> None:
    vector = [1.0, 0.0, 0.0]
    assert cosine_similarity(vector, vector) == pytest.approx(1.0)


def test_cosine_similarity_orthogonal_vectors() -> None:
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_mean_max_cosine_similarity_uses_best_reference_match() -> None:
    query = [[1.0, 0.0], [0.0, 1.0]]
    reference = [[1.0, 0.0], [0.5, 0.5]]
    assert mean_max_cosine_similarity(query, reference) == pytest.approx(0.8535533905932737)


@pytest.mark.asyncio
async def test_validate_redline_accepts_when_similarity_meets_threshold(monkeypatch) -> None:
    from tests.test_redline_generation import sample_policy_docx_bytes

    policy_chunk = DocumentChunk(
        id='chunk-1',
        org_id='org_example',
        policy_id='policy-1',
        chunk_index=0,
        content='Policy body text about communications.',
        embedding=[1.0, 0.0, 0.0],
        metadata_json={},
    )

    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[DocumentChunk]:
            return [policy_chunk]

    class FakeSession:
        def query(self, _model: object) -> FakeQuery:
            return FakeQuery()

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.redline_vector_validation_service.generate_embeddings',
        fake_generate_embeddings,
    )

    result = await validate_redline_against_internal_policy(
        FakeSession(),  # type: ignore[arg-type]
        policy_id='policy-1',
        org_id='org_example',
        policy_name='Communications Policy',
        redline_bytes=sample_policy_docx_bytes(),
        internal_policy_text='Policy body text about communications.',
        settings=Settings(
            OPENAI_API_KEY='test-key',
            REDLINE_MIN_VECTOR_SIMILARITY=0.25,
        ),
    )

    assert result.accepted is True
    assert result.similarity >= 0.25


@pytest.mark.asyncio
async def test_validate_redline_rejects_when_similarity_below_threshold(monkeypatch) -> None:
    from tests.test_redline_generation import sample_policy_docx_bytes

    policy_chunk = DocumentChunk(
        id='chunk-1',
        org_id='org_example',
        policy_id='policy-1',
        chunk_index=0,
        content='Policy body text about communications.',
        embedding=[1.0, 0.0, 0.0],
        metadata_json={},
    )

    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[DocumentChunk]:
            return [policy_chunk]

    class FakeSession:
        def query(self, _model: object) -> FakeQuery:
            return FakeQuery()

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.0, 1.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.redline_vector_validation_service.generate_embeddings',
        fake_generate_embeddings,
    )

    result = await validate_redline_against_internal_policy(
        FakeSession(),  # type: ignore[arg-type]
        policy_id='policy-1',
        org_id='org_example',
        policy_name='Communications Policy',
        redline_bytes=sample_policy_docx_bytes(),
        internal_policy_text='Policy body text about communications.',
        settings=Settings(
            OPENAI_API_KEY='test-key',
            REDLINE_MIN_VECTOR_SIMILARITY=0.25,
        ),
    )

    assert result.accepted is False
    assert result.similarity < 0.25


@pytest.mark.asyncio
async def test_validate_redline_against_regulators_accepts_above_threshold(monkeypatch) -> None:
    from app.services.redline_vector_validation_service import (
        validate_redline_against_regulator_documents,
    )
    from tests.test_redline_generation import sample_policy_docx_bytes

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[1.0, 0.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.redline_vector_validation_service.generate_embeddings',
        fake_generate_embeddings,
    )

    result = await validate_redline_against_regulator_documents(
        None,
        org_id='org_example',
        policy_name='Communications Policy',
        redline_bytes=sample_policy_docx_bytes(),
        fallback_text='Policy body text about communications.',
        settings=Settings(
            OPENAI_API_KEY='test-key',
            REDLINE_MIN_VECTOR_SIMILARITY=0.25,
        ),
        regulator_vectors=[[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.8, 0.2, 0.0]],
    )

    assert result.accepted is True
    assert result.similarity >= 0.25
    assert result.redline_vectors


@pytest.mark.asyncio
async def test_validate_redline_against_regulators_rejects_below_threshold(monkeypatch) -> None:
    from app.services.redline_vector_validation_service import (
        validate_redline_against_regulator_documents,
    )
    from tests.test_redline_generation import sample_policy_docx_bytes

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.0, 1.0, 0.0] for _ in chunks]

    monkeypatch.setattr(
        'app.services.redline_vector_validation_service.generate_embeddings',
        fake_generate_embeddings,
    )

    result = await validate_redline_against_regulator_documents(
        None,
        org_id='org_example',
        policy_name='Communications Policy',
        redline_bytes=sample_policy_docx_bytes(),
        fallback_text='Policy body text about communications.',
        settings=Settings(
            OPENAI_API_KEY='test-key',
            REDLINE_MIN_VECTOR_SIMILARITY=0.25,
        ),
        regulator_vectors=[[1.0, 0.0, 0.0]],
    )

    assert result.accepted is False
    assert result.similarity < 0.25


@pytest.mark.asyncio
async def test_finalize_policy_redline_rejects_after_max_validation_retries(monkeypatch) -> None:
    from tests.test_redline_generation import RedlineDraft, sample_context

    context = sample_context()
    revision = SimpleNamespace(
        id='revision-1',
        redline_file_url=None,
        gap_analysis_id='gap-id',
        policy_id='policy-id',
        org_id='org_example',
        status='PENDING',
    )
    draft_calls = 0
    validate_calls = 0

    async def fake_draft_replacement_language(*_args: object, **_kwargs: object) -> RedlineDraft:
        nonlocal draft_calls
        draft_calls += 1
        return RedlineDraft(sections=[], drafting_summary='test')

    async def fake_validate(*_args: object, **_kwargs: object) -> RedlineVectorValidationResult:
        nonlocal validate_calls
        validate_calls += 1
        return RedlineVectorValidationResult(
            accepted=False,
            similarity=0.1,
            threshold=0.25,
        )

    monkeypatch.setattr(
        'app.services.redline_generation_service.draft_replacement_language',
        fake_draft_replacement_language,
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.validate_redline_against_regulator_documents',
        fake_validate,
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.load_all_regulator_reference_embeddings',
        lambda *_args, **_kwargs: [[1.0, 0.0, 0.0]],
    )

    class SessionStub:
        def commit(self) -> None:
            return None

        def refresh(self, _value: object) -> None:
            return None

        def rollback(self) -> None:
            return None

    with pytest.raises(HTTPException) as exc_info:
        await finalize_policy_redline_for_revision(
            revision,  # type: ignore[arg-type]
            context,
            SessionStub(),  # type: ignore[arg-type]
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == NO_RELATED_REDLINE_CONTENT_MESSAGE
    assert draft_calls == 5
    assert validate_calls == 5


@pytest.mark.asyncio
async def test_finalize_policy_redline_succeeds_on_validation_retry(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.models.core import PolicyRevision
    from tests.test_redline_generation import RedlineDraft, sample_context

    context = sample_context()
    revision = PolicyRevision(
        id='revision-1',
        redline_file_url=None,
        gap_analysis_id='gap-id',
        policy_id='policy-id',
        org_id='org_example',
        status='PENDING',
        created_at=datetime.now(UTC),
    )
    validate_calls = 0

    async def fake_draft_replacement_language(*_args: object, **_kwargs: object) -> RedlineDraft:
        return RedlineDraft(sections=[], drafting_summary='test')

    async def fake_validate(*_args: object, **_kwargs: object) -> RedlineVectorValidationResult:
        nonlocal validate_calls
        validate_calls += 1
        if validate_calls < 2:
            return RedlineVectorValidationResult(
                accepted=False,
                similarity=0.1,
                threshold=0.25,
            )
        return RedlineVectorValidationResult(
            accepted=True,
            similarity=0.9,
            threshold=0.25,
        )

    class SessionStub:
        def commit(self) -> None:
            return None

        def refresh(self, _value: object) -> None:
            return None

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        'app.services.redline_generation_service.draft_replacement_language',
        fake_draft_replacement_language,
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.validate_redline_against_regulator_documents',
        fake_validate,
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.load_all_regulator_reference_embeddings',
        lambda *_args, **_kwargs: [[1.0, 0.0, 0.0]],
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.upload_file',
        lambda *_args, **_kwargs: SimpleNamespace(uri='storage://policy-redlines/test.docx'),
    )
    monkeypatch.setattr(
        'app.services.redline_generation_service.notify_new_revision',
        AsyncMock(),
    )

    response = await finalize_policy_redline_for_revision(
        revision,
        context,
        SessionStub(),  # type: ignore[arg-type]
    )

    assert validate_calls == 2
    assert response.redline_file_url == 'storage://policy-redlines/test.docx'
