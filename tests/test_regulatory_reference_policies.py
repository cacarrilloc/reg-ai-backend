from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.config import Settings
from app.models.core import Organization, Policy, Regulation, RegulatoryPublication
from app.services.regulatory_monitoring_service import is_sec_edgar_filing_url
from app.services.regulatory_reference_policy_service import (
    REGULATORY_REFERENCE_TYPE,
    active_internal_regulator_sources,
    build_bootstrap_reference_text,
    build_reference_document_text,
    collect_regulatory_scrape_sources,
    content_hash,
    org_reference_sources,
    reference_policy_name,
    regulator_sources_for_policy_ids,
    sync_regulatory_reference_policies,
)


def test_reference_policy_name_for_known_sources() -> None:
    assert reference_policy_name('SEC') == 'SEC Regulations'
    assert reference_policy_name('finra') == 'FINRA Regulations'
    assert reference_policy_name('CFTC') == 'CFTC Regulations'


def test_is_sec_edgar_filing_url_for_reference_filtering() -> None:
    assert is_sec_edgar_filing_url(
        "https://www.sec.gov/Archives/edgar/data/320193/0001/aapl.htm"
    )
    assert not is_sec_edgar_filing_url(
        "https://www.sec.gov/newsroom/speeches-statements/example"
    )


def test_build_reference_document_text_includes_publication_metadata() -> None:
    publication = RegulatoryPublication(
        id='pub-1',
        source='SEC',
        title='Sample Notice',
        url='https://www.sec.gov/newsroom/example',
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        raw_text='Important regulatory update.',
        processed=False,
        embedding_chunk_ids=[],
    )

    document = build_reference_document_text([publication])

    assert 'Sample Notice' in document
    assert 'https://www.sec.gov/newsroom/example' in document
    assert 'Important regulatory update.' in document


@pytest.mark.asyncio
async def test_sync_regulatory_reference_policies_creates_org_reference_policy(monkeypatch) -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['SEC']},
    )
    publication = RegulatoryPublication(
        id='pub-1',
        source='SEC',
        title='SEC Seeks Comment',
        url='https://www.sec.gov/newsroom/example',
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        raw_text='The SEC published a new notice.',
        processed=False,
        embedding_chunk_ids=[],
    )

    class FakeQuery:
        def __init__(self, model: object, items: list[object]) -> None:
            self.model = model
            self.items = items

        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def limit(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def first(self) -> object | None:
            if self.model is Regulation:
                return None
            return None

        def all(self) -> list[object]:
            if self.items:
                return self.items
            if self.model is Organization:
                return [organization]
            if self.model is RegulatoryPublication:
                return [publication]
            if self.model is object:
                return []
            return []

        def update(self, *_args: object, **_kwargs: object) -> int:
            return 0

        def delete(self, *_args: object, **_kwargs: object) -> int:
            return 0

        def scalar(self) -> int | None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, model: object) -> FakeQuery:
            if model is Policy.name:
                return FakeQuery(Policy, [('SEC',)])
            if getattr(model, '__name__', None) == 'max':
                return FakeQuery(model, [])
            return FakeQuery(model, [])

        def add(self, value: object) -> None:
            self.added.append(value)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.1, 0.2] for _ in chunks]

    db = FakeSession()
    settings = Settings(REGULATORY_MAX_ITEMS_PER_FEED=5)

    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.generate_embeddings',
        fake_generate_embeddings,
    )
    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.next_regulation_version',
        lambda *_args, **_kwargs: 1,
    )
    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.store_reference_policy_file',
        lambda **_kwargs: None,
    )

    updated = await sync_regulatory_reference_policies(db, ['SEC'], settings)

    assert updated == 1
    policies = [item for item in db.added if isinstance(item, Regulation) and item.rule_code is None]
    assert len(policies) == 1
    assert policies[0].name == 'SEC Regulations'
    assert policies[0].type == REGULATORY_REFERENCE_TYPE
    assert policies[0].file_url == 'https://www.sec.gov/newsroom'


@pytest.mark.asyncio
async def test_sync_skips_when_reference_content_is_unchanged(monkeypatch) -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['SEC']},
    )
    publication = RegulatoryPublication(
        id='pub-1',
        source='SEC',
        title='SEC Seeks Comment',
        url='https://www.sec.gov/newsroom/example',
        published_at=datetime(2026, 6, 1, tzinfo=UTC),
        raw_text='The SEC published a new notice.',
        processed=False,
        embedding_chunk_ids=[],
    )
    document_text = build_reference_document_text([publication])
    existing_policy = Regulation(
        id='policy-1',
        org_id='org_example',
        name='SEC Regulations',
        type=REGULATORY_REFERENCE_TYPE,
        version=1,
        status='ACTIVE',
        file_url='storage://policy-documents/org_example/policies/policy-1/SEC_Regulations.docx',
    )

    class FakeQuery:
        def __init__(self, model: object) -> None:
            self.model = model

        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def limit(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def first(self) -> object | None:
            if self.model is Regulation:
                return existing_policy
            return None

        def all(self) -> list[object]:
            if self.model is Organization:
                return [organization]
            if self.model is RegulatoryPublication:
                return [publication]
            return []

        def delete(self, *_args: object, **_kwargs: object) -> int:
            return 0

        def update(self, *_args: object, **_kwargs: object) -> int:
            return 0

    class FakeSession:
        def query(self, model: object) -> FakeQuery:
            return FakeQuery(model)

    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.active_reference_content_hash',
        lambda _db, _policy_id: content_hash(document_text),
    )

    embed_calls = {'count': 0}

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        embed_calls['count'] += 1
        return [[0.1, 0.2] for _ in chunks]

    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.generate_embeddings',
        fake_generate_embeddings,
    )

    updated = await sync_regulatory_reference_policies(
        FakeSession(),  # type: ignore[arg-type]
        ['SEC'],
        Settings(REGULATORY_MAX_ITEMS_PER_FEED=5),
    )

    assert updated == 0
    assert embed_calls['count'] == 0


@pytest.mark.asyncio
async def test_sync_creates_finra_bootstrap_when_no_publications(monkeypatch) -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    class FakeQuery:
        def __init__(self, model: object) -> None:
            self.model = model

        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def limit(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def first(self) -> object | None:
            return None

        def all(self) -> list[object]:
            if self.model is Regulation:
                return [('FINRA',)]
            if self.model is Organization:
                return [organization]
            if self.model is RegulatoryPublication:
                return []
            return []

        def update(self, *_args: object, **_kwargs: object) -> int:
            return 0

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, model: object) -> FakeQuery:
            if model is Policy.name:
                return FakeQuery(Policy)
            return FakeQuery(model)

        def add(self, value: object) -> None:
            self.added.append(value)

        def flush(self) -> None:
            return None

        def commit(self) -> None:
            return None

        def rollback(self) -> None:
            return None

    async def fake_generate_embeddings(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.1, 0.2] for _ in chunks]

    db = FakeSession()
    settings = Settings(REGULATORY_MAX_ITEMS_PER_FEED=5)

    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.generate_embeddings',
        fake_generate_embeddings,
    )
    monkeypatch.setattr(
        'app.services.regulatory_reference_policy_service.next_regulation_version',
        lambda *_args, **_kwargs: 1,
    )

    updated = await sync_regulatory_reference_policies(db, None, settings)

    assert updated == 3
    policies = [item for item in db.added if isinstance(item, Regulation) and item.rule_code is None]
    assert {policy.name for policy in policies} == {
        'FINRA Regulations',
        'SEC Regulations',
        'CFTC Regulations',
    }
    assert all(policy.type == REGULATORY_REFERENCE_TYPE for policy in policies)


def test_build_bootstrap_reference_text_mentions_source() -> None:
    text = build_bootstrap_reference_text('FINRA')
    assert 'FINRA Regulations' in text
    assert 'https://www.finra.org/media-center' in text


def test_org_reference_sources_always_returns_all_regulators() -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[tuple[str]]:
            return [('AML Policy',), ('Written Supervisory Procedures',)]

    class FakeSession:
        def query(self, _model: object) -> FakeQuery:
            return FakeQuery()

    sources = org_reference_sources(organization, FakeSession())  # type: ignore[arg-type]

    assert sources == ['FINRA', 'SEC', 'CFTC']


def test_org_reference_sources_without_db_still_returns_all_regulators() -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['FINRA']},
    )

    assert org_reference_sources(organization) == ['FINRA', 'SEC', 'CFTC']


def test_active_internal_regulator_sources_returns_only_regulator_policy_names() -> None:
    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[tuple[str]]:
            return [('SEC',), ('AML Policy',)]

    class FakeSession:
        def query(self, _model: object) -> FakeQuery:
            return FakeQuery()

    assert active_internal_regulator_sources(FakeSession(), 'org_example') == {'SEC'}  # type: ignore[arg-type]


def test_regulator_sources_for_policy_ids_returns_uploaded_regulator_only() -> None:
    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[tuple[str]]:
            return [('FINRA',)]

    class FakeSession:
        def query(self, _model: object) -> FakeQuery:
            return FakeQuery()

    assert regulator_sources_for_policy_ids(FakeSession(), 'org_example', ['policy-finra']) == ['FINRA']  # type: ignore[arg-type]


def test_collect_regulatory_scrape_sources_for_org() -> None:
    organization = Organization(
        id='org-row-id',
        auth_org_id='org_example',
        name='Example Capital',
        regulatory_scope={'sources': ['SEC', 'CFTC']},
    )

    class FakeOrgQuery:
        def all(self) -> list[Organization]:
            return [organization]

    class FakePolicyQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakePolicyQuery:
            return self

        def all(self) -> list[tuple[str]]:
            return [('FINRA',)]

    class FakeSession:
        def query(self, model: object) -> object:
            if model is Organization:
                return FakeOrgQuery()
            return FakePolicyQuery()

    assert collect_regulatory_scrape_sources(FakeSession(), 'org_example') == ['FINRA', 'SEC', 'CFTC']  # type: ignore[arg-type]


def test_prune_regulatory_reference_versions_keeps_newest_ten() -> None:
    from app.services.regulatory_reference_policy_service import prune_regulatory_reference_versions

    policies = [
        Regulation(
            id=f'p-{version}',
            org_id='org_example',
            name='SEC Regulations',
            type=REGULATORY_REFERENCE_TYPE,
            version=version,
            status='ACTIVE' if version == 12 else 'SUPERSEDED',
        )
        for version in range(1, 13)
    ]

    class FakeQuery:
        def __init__(self, session: FakeSession, model: object) -> None:
            self.session = session
            self.model = model

        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[object]:
            if self.model is Regulation:
                return sorted(self.session.policies, key=lambda item: item.version, reverse=True)
            return []

        def delete(self, synchronize_session: bool = False) -> int:
            del synchronize_session
            before = len(self.session.policies)
            self.session.policies = [
                policy
                for policy in self.session.policies
                if policy.id not in self.session.pending_delete_ids
            ]
            return before - len(self.session.policies)

    class FakeSession:
        def __init__(self) -> None:
            self.policies = list(policies)
            self.pending_delete_ids: set[str] = set()

        def query(self, model: object) -> FakeQuery:
            return FakeQuery(self, model)

    db = FakeSession()

    def fake_delete_chunks(_db: FakeSession, policy_ids: list[str] | set[str]) -> int:
        db.pending_delete_ids = set(policy_ids)
        return len(policy_ids)

    import app.services.regulatory_reference_policy_service as service

    original = service.delete_chunks_for_regulation_ids
    service.delete_chunks_for_regulation_ids = fake_delete_chunks  # type: ignore[assignment]
    try:
        deleted = prune_regulatory_reference_versions(
            db,  # type: ignore[arg-type]
            org_id='org_example',
            source='SEC',
            keep=10,
        )
    finally:
        service.delete_chunks_for_regulation_ids = original  # type: ignore[assignment]

    assert deleted == 2
    remaining_versions = sorted(policy.version for policy in db.policies)
    assert remaining_versions == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    assert len(db.policies) == 10


def test_prune_regulatory_reference_versions_hard_caps_with_extra_active() -> None:
    from app.services.regulatory_reference_policy_service import prune_regulatory_reference_versions

    policies = [
        Regulation(
            id=f'p-{version}',
            org_id='org_example',
            name='SEC Regulations' if version > 5 else 'SEC Latest Regulations',
            type=REGULATORY_REFERENCE_TYPE,
            version=version,
            # Two ACTIVE rows — old prune logic retained both and exceeded keep=10.
            status='ACTIVE' if version in {1, 15} else 'SUPERSEDED',
        )
        for version in range(1, 16)
    ]

    class FakeQuery:
        def __init__(self, session: FakeSession, model: object) -> None:
            self.session = session
            self.model = model

        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def all(self) -> list[object]:
            if self.model is Regulation:
                return sorted(self.session.policies, key=lambda item: item.version, reverse=True)
            return []

        def delete(self, synchronize_session: bool = False) -> int:
            del synchronize_session
            before = len(self.session.policies)
            self.session.policies = [
                policy
                for policy in self.session.policies
                if policy.id not in self.session.pending_delete_ids
            ]
            return before - len(self.session.policies)

    class FakeSession:
        def __init__(self) -> None:
            self.policies = list(policies)
            self.pending_delete_ids: set[str] = set()

        def query(self, model: object) -> FakeQuery:
            return FakeQuery(self, model)

    db = FakeSession()

    def fake_delete_chunks(_db: FakeSession, policy_ids: list[str] | set[str]) -> int:
        db.pending_delete_ids = set(policy_ids)
        return len(policy_ids)

    import app.services.regulatory_reference_policy_service as service

    original = service.delete_chunks_for_regulation_ids
    service.delete_chunks_for_regulation_ids = fake_delete_chunks  # type: ignore[assignment]
    try:
        deleted = prune_regulatory_reference_versions(
            db,  # type: ignore[arg-type]
            org_id='org_example',
            source='SEC',
            keep=10,
        )
    finally:
        service.delete_chunks_for_regulation_ids = original  # type: ignore[assignment]

    assert deleted == 5
    assert len(db.policies) == 10
    assert max(policy.version for policy in db.policies) == 15
    assert sum(1 for policy in db.policies if policy.status == 'ACTIVE') == 1
    assert any(policy.status == 'ACTIVE' for policy in db.policies)
