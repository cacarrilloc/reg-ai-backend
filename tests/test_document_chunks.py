from __future__ import annotations

import pytest

from app.models.core import Policy, Regulation
from app.services.document_chunk_service import (
    chunk_policy_name_value,
    compare_internal_policy_to_regulator_documents,
    compare_redline_to_internal_policy,
    compare_redline_to_regulator_documents,
    redline_internal_similarity_fields,
    redline_regulator_similarity_fields,
    redline_storage_chunk_index,
    should_retry_redline_similarity_backfill,
)
from app.services.policy_types import (
    INTERNAL_POLICY_TYPE,
    REGULATORY_REFERENCE_TYPE,
    chunk_document_type_for_policy,
    normalize_chunk_policy_name,
)


def test_chunk_document_type_for_policy() -> None:
    assert chunk_document_type_for_policy('uploaded') == 'uploaded'
    assert chunk_document_type_for_policy('WSP') == 'uploaded'
    assert chunk_document_type_for_policy('REGULATORY_REFERENCE') == 'REGULATORY_REFERENCE'
    assert chunk_document_type_for_policy(None) == 'uploaded'


def test_normalize_chunk_policy_name() -> None:
    assert normalize_chunk_policy_name('AML Policy', INTERNAL_POLICY_TYPE) == 'AML Policy'
    assert normalize_chunk_policy_name('FINRA', INTERNAL_POLICY_TYPE) == 'FINRA'
    assert normalize_chunk_policy_name('FINRA Media Center Reference', REGULATORY_REFERENCE_TYPE) == 'FINRA'
    assert normalize_chunk_policy_name('SEC Newsroom Reference', REGULATORY_REFERENCE_TYPE) == 'SEC'


def test_chunk_policy_name_value_uses_regulator_source() -> None:
    assert chunk_policy_name_value(regulator_source='cftc') == 'CFTC'


def test_compare_internal_policy_to_regulator_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    internal_policy = Policy(
        id='internal-1',
        org_id='org_example',
        name='FINRA',
        type=INTERNAL_POLICY_TYPE,
        version=1,
        status='ACTIVE',
    )
    finra_reference = Regulation(
        id='ref-finra',
        org_id='org_example',
        name='FINRA Media Center Reference',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )
    sec_reference = Regulation(
        id='ref-sec',
        org_id='org_example',
        name='SEC Regulations',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )
    cftc_reference = Regulation(
        id='ref-cftc',
        org_id='org_example',
        name='CFTC Regulations',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )

    def fake_load(
        _db: object,
        *,
        org_id: str,
        policy_id: str | None = None,
        regulation_id: str | None = None,
        document_type: str | None = None,
        policy_name: str | None = None,
    ) -> list[list[float]]:
        del org_id, document_type, policy_name
        doc_id = regulation_id or policy_id
        if doc_id == 'internal-1':
            return [[1.0, 0.0, 0.0]]
        if doc_id == 'ref-finra':
            return [[1.0, 0.0, 0.0]]
        if doc_id == 'ref-sec':
            return [[0.0, 1.0, 0.0]]
        if doc_id == 'ref-cftc':
            return [[0.0, 0.0, 1.0]]
        return []

    monkeypatch.setattr(
        'app.services.document_chunk_service.load_embedded_vectors',
        fake_load,
    )

    class PolicyQuery:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object, **_kwargs: object) -> PolicyQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> PolicyQuery:
            return self

        def one_or_none(self) -> object | None:
            return self.result

        def all(self) -> list[object]:
            return self.result if isinstance(self.result, list) else []

    class SessionStub:
        def __init__(self) -> None:
            self.policy_step = 0

        def query(self, model: object) -> PolicyQuery:
            if model is Policy:
                return PolicyQuery(internal_policy)
            if model is Regulation:
                return PolicyQuery([finra_reference, sec_reference, cftc_reference])
            return PolicyQuery([])

    results = compare_internal_policy_to_regulator_documents(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        internal_policy_id='internal-1',
        internal_policy_name='FINRA',
    )

    assert len(results) == 4
    assert results[0].regulator_source == 'ALL'
    assert {item.regulator_source for item in results} == {'ALL', 'FINRA', 'SEC', 'CFTC'}
    finra_result = next(item for item in results if item.regulator_source == 'FINRA')
    assert finra_result.document_type == REGULATORY_REFERENCE_TYPE
    assert finra_result.cosine_similarity == pytest.approx(1.0)


def test_compare_redline_to_internal_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_load_internal(
        _db: object,
        *,
        org_id: str,
        policy_id: str,
        policy_name: str,
    ) -> list[list[float]]:
        del org_id, policy_id, policy_name
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    monkeypatch.setattr(
        'app.services.document_chunk_service.load_internal_policy_vectors',
        fake_load_internal,
    )

    results = compare_redline_to_internal_policy(
        object(),  # type: ignore[arg-type]
        org_id='org_example',
        policy_id='internal-1',
        policy_name='FINRA',
        redline_vectors=[[1.0, 0.0, 0.0]],
    )

    assert len(results) == 1
    assert results[0].policy_name == 'FINRA'
    assert results[0].document_type == INTERNAL_POLICY_TYPE
    assert results[0].cosine_similarity == pytest.approx(1.0)


def test_redline_internal_similarity_fields_extracts_primary_match() -> None:
    fields = redline_internal_similarity_fields(
        {
            'redline_internal_document_similarity': [
                {
                    'policy_name': 'FINRA',
                    'cosine_similarity': 0.734,
                }
            ]
        }
    )

    assert fields['redline_internal_cosine_similarity'] == pytest.approx(0.734)
    assert fields['redline_internal_policy_name'] == 'FINRA'


def test_compare_redline_to_regulator_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    finra_reference = Regulation(
        id='ref-finra',
        org_id='org_example',
        name='FINRA Media Center Reference',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )
    sec_reference = Regulation(
        id='ref-sec',
        org_id='org_example',
        name='SEC Newsroom Reference',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )

    def fake_load(
        _db: object,
        *,
        org_id: str,
        policy_id: str | None = None,
        regulation_id: str | None = None,
        document_type: str | None = None,
        policy_name: str | None = None,
    ) -> list[list[float]]:
        del org_id, document_type, policy_name
        doc_id = regulation_id or policy_id
        if doc_id == 'ref-finra':
            return [[1.0, 0.0, 0.0]]
        if doc_id == 'ref-sec':
            return [[0.0, 1.0, 0.0]]
        return []

    monkeypatch.setattr(
        'app.services.document_chunk_service.load_embedded_vectors',
        fake_load,
    )

    class PolicyQuery:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object, **_kwargs: object) -> PolicyQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> PolicyQuery:
            return self

        def all(self) -> list[object]:
            return self.result if isinstance(self.result, list) else []

    class SessionStub:
        def query(self, model: object) -> PolicyQuery:
            if model is Regulation:
                return PolicyQuery([finra_reference, sec_reference])
            return PolicyQuery([])

    results = compare_redline_to_regulator_documents(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        policy_name='AML Policy',
        redline_vectors=[[1.0, 0.0, 0.0]],
    )

    assert len(results) == 3
    assert results[0].regulator_source == 'ALL'
    assert results[0].policy_name == 'FINRA, SEC, and CFTC Regulations'
    assert {item.regulator_source for item in results[1:]} == {'FINRA', 'SEC'}
    finra_result = next(item for item in results if item.regulator_source == 'FINRA')
    assert finra_result.document_type == REGULATORY_REFERENCE_TYPE
    assert finra_result.cosine_similarity == pytest.approx(1.0)


def test_redline_regulator_similarity_fields_prefers_overall_all_regulators_score() -> None:
    fields = redline_regulator_similarity_fields(
        {
            'redline_regulator_document_similarity': [
                {
                    'regulator_source': 'ALL',
                    'policy_name': 'FINRA, SEC, and CFTC Regulations',
                    'cosine_similarity': 0.42,
                },
                {
                    'regulator_source': 'SEC',
                    'policy_name': 'SEC Newsroom Reference',
                    'cosine_similarity': 0.9,
                },
            ]
        }
    )

    assert fields['redline_regulator_cosine_similarity'] == pytest.approx(0.42)
    assert fields['redline_regulator_reference_policy_name'] == (
        'FINRA, SEC, and CFTC Regulations'
    )


def test_redline_regulator_similarity_fields_extracts_primary_match() -> None:
    fields = redline_regulator_similarity_fields(
        {
            'redline_regulator_document_similarity': [
                {
                    'regulator_source': 'SEC',
                    'policy_name': 'SEC Newsroom Reference',
                    'cosine_similarity': 0.654321,
                }
            ]
        }
    )

    assert fields['redline_regulator_cosine_similarity'] == pytest.approx(0.654321)
    assert fields['redline_regulator_reference_policy_name'] == 'SEC Newsroom Reference'


def test_redline_storage_chunk_index_avoids_internal_policy_indices() -> None:
    gap_analysis_id = 'e2dc84ad-9a8e-4a19-a6fb-8ea01d9f788d'

    assert redline_storage_chunk_index(gap_analysis_id, 0) >= 10_000_000
    assert redline_storage_chunk_index(gap_analysis_id, 0) != 0
    assert redline_storage_chunk_index(gap_analysis_id, 1) == (
        redline_storage_chunk_index(gap_analysis_id, 0) + 1
    )


def test_should_retry_redline_similarity_backfill_respects_attempt_limit() -> None:
    assert should_retry_redline_similarity_backfill({}) is True
    assert should_retry_redline_similarity_backfill(
        {'redline_similarity_backfill_attempts': 3}
    ) is False
