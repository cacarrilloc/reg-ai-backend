from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.core import AuditLog, ComplianceHistory, GapAnalysis, Policy, PolicyRevision
from app.services.policy_service import delete_internal_policy_for_org
from app.services.policy_types import MARKETING_CONTENT_TYPE, REGULATORY_REFERENCE_TYPE


class _FilterQuery:
    def __init__(self, rows: list[object] | None = None, *, one: object | None = None):
        self.rows = list(rows or [])
        self.one = one
        self.updated: dict[object, object] | None = None
        self.deleted = False

    def filter(self, *_args: object, **_kwargs: object):
        return self

    def one_or_none(self):
        return self.one

    def all(self):
        return list(self.rows)

    def update(self, values: dict[object, object], synchronize_session: object = False):
        del synchronize_session
        self.updated = values
        return len(self.rows)

    def delete(self, synchronize_session: object = False):
        del synchronize_session
        self.deleted = True
        return len(self.rows)


class _Session:
    def __init__(self, queries: dict[object, _FilterQuery]):
        self.queries = queries
        self.deleted: list[object] = []
        self.committed = False
        self.rolled_back = False

    def query(self, *entities: object) -> _FilterQuery:
        first = entities[0] if entities else None
        query = self.queries.get(first)
        if query is None:
            query = _FilterQuery()
            if first is not None:
                self.queries[first] = query
        return query

    def delete(self, obj: object) -> None:
        self.deleted.append(obj)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _internal_policy(*, policy_id: str = 'policy-v2', name: str = 'AML Test Policy') -> Policy:
    return Policy(
        id=policy_id,
        org_id='org_example',
        name=name,
        type='uploaded',
        version=2,
        status='ACTIVE',
        file_url='storage://policies/org_example/policies/policy-v2/aml.docx',
    )


def test_delete_internal_policy_removes_versions_gaps_and_reviews(monkeypatch) -> None:
    current = _internal_policy()
    older = Policy(
        id='policy-v1',
        org_id='org_example',
        name='AML Test Policy',
        type='uploaded',
        version=1,
        status='SUPERSEDED',
        file_url='storage://policies/org_example/policies/policy-v1/aml.docx',
    )
    gap = GapAnalysis(
        id='gap-aml',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-v2',
        policy_id='policy-v2',
        impact_score='HIGH',
        gap_summary='Missing independent testing',
        retrieval_metadata={},
        analysis_mode='ANTHROPIC',
    )
    queries = {
        Policy: _FilterQuery(rows=[current, older], one=current),
        GapAnalysis: _FilterQuery(rows=[gap]),
        AuditLog: _FilterQuery(),
        ComplianceHistory: _FilterQuery(),
        PolicyRevision: _FilterQuery(),
    }
    session = _Session(queries)
    removed_urls: list[str] = []

    monkeypatch.setattr(
        'app.services.policy_service.delete_chunks_for_policy_ids',
        lambda db, policy_ids: len(list(policy_ids)),
    )
    monkeypatch.setattr(
        'app.services.policy_service.parse_storage_uri',
        lambda file_url: SimpleNamespace(uri=file_url),
    )
    monkeypatch.setattr(
        'app.services.policy_service.remove_file',
        lambda stored: removed_urls.append(stored.uri),
    )

    deleted = delete_internal_policy_for_org(session, 'org_example', 'policy-v2')

    assert deleted is True
    assert session.committed is True
    assert session.deleted == []
    assert queries[GapAnalysis].deleted is True
    assert queries[Policy].deleted is True
    assert queries[PolicyRevision].deleted is True
    assert queries[ComplianceHistory].updated == {ComplianceHistory.policy_id: None}
    assert set(removed_urls) == {
        'storage://policies/org_example/policies/policy-v2/aml.docx',
        'storage://policies/org_example/policies/policy-v1/aml.docx',
    }


def test_delete_internal_policy_removes_case_variant_versions(monkeypatch) -> None:
    current = _internal_policy()
    older = Policy(
        id='policy-v1',
        org_id='org_example',
        name='aml test policy',
        type='uploaded',
        version=1,
        status='SUPERSEDED',
        file_url='storage://policies/org_example/policies/policy-v1/aml.docx',
    )
    queries = {
        Policy: _FilterQuery(rows=[current, older], one=current),
        GapAnalysis: _FilterQuery(rows=[]),
        AuditLog: _FilterQuery(),
        PolicyRevision: _FilterQuery(),
    }
    session = _Session(queries)
    monkeypatch.setattr(
        'app.services.policy_service.delete_chunks_for_policy_ids',
        lambda db, policy_ids: len(list(policy_ids)),
    )
    monkeypatch.setattr(
        'app.services.policy_service.parse_storage_uri',
        lambda file_url: SimpleNamespace(uri=file_url),
    )
    monkeypatch.setattr('app.services.policy_service.remove_file', lambda stored: None)

    deleted = delete_internal_policy_for_org(session, 'org_example', 'policy-v2')

    assert deleted is True
    assert queries[Policy].deleted is True


def test_delete_internal_policy_rejects_regulations() -> None:
    regulation = Policy(
        id='gov-finra',
        org_id='org_example',
        name='FINRA Regulations',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
    )
    session = _Session({Policy: _FilterQuery(one=regulation)})

    with pytest.raises(HTTPException) as caught:
        delete_internal_policy_for_org(session, 'org_example', 'gov-finra')

    assert caught.value.status_code == 400
    assert session.committed is False


def test_delete_internal_policy_rejects_marketing_content() -> None:
    marketing = Policy(
        id='marketing-1',
        org_id='org_example',
        name='Brochure',
        type=MARKETING_CONTENT_TYPE,
        version=1,
        status='ACTIVE',
    )
    session = _Session({Policy: _FilterQuery(one=marketing)})

    with pytest.raises(HTTPException) as caught:
        delete_internal_policy_for_org(session, 'org_example', 'marketing-1')

    assert caught.value.status_code == 400


def test_delete_internal_policy_returns_false_when_missing() -> None:
    session = _Session({Policy: _FilterQuery(one=None)})

    assert delete_internal_policy_for_org(session, 'org_example', 'missing') is False


def test_delete_internal_policy_maps_sql_errors_before_commit() -> None:
    from sqlalchemy.exc import IntegrityError

    current = _internal_policy()

    class BoomPolicyQuery(_FilterQuery):
        def delete(self, synchronize_session: object = False):
            del synchronize_session
            raise IntegrityError('DELETE FROM policies', {}, Exception('fk'))

    session = _Session(
        {
            Policy: BoomPolicyQuery(rows=[current], one=current),
            GapAnalysis: _FilterQuery(rows=[]),
        }
    )

    with pytest.raises(HTTPException) as caught:
        delete_internal_policy_for_org(session, 'org_example', 'policy-v2')

    assert caught.value.status_code == 500
    assert caught.value.detail == 'Unable to delete internal policy'
    assert session.rolled_back is True
    assert session.committed is False


def test_delete_internal_policy_requires_database() -> None:
    with pytest.raises(HTTPException) as caught:
        delete_internal_policy_for_org(None, 'org_example', 'policy-1')

    assert caught.value.status_code == 503
