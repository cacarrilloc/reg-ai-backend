from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.models.core import DocumentChunk, Regulation
from app.services.marketing_regulation_sync import sync_marketing_regulation_documents
from app.services.marketing_rule_corpus import RULE_SPECS, fallback_rule_document
from app.services.policy_types import REGULATORY_REFERENCE_TYPE


def _catalog_rows() -> list[Regulation]:
    return [
        Regulation(
            id='id-206(4)-1',
            org_id='org_example',
            name='SEC: Rule 206(4)-1: The Marketing Rule',
            type=REGULATORY_REFERENCE_TYPE,
            version=1,
            status='ACTIVE',
            rule_code='206(4)-1',
        ),
        Regulation(
            id='id-204-2',
            org_id='org_example',
            name='SEC: Rule 204-2: Recordkeeping',
            type=REGULATORY_REFERENCE_TYPE,
            version=1,
            status='ACTIVE',
            rule_code='204-2',
        ),
        Regulation(
            id='id-2210',
            org_id='org_example',
            name='FINRA: Rule 2210: Communications with the public',
            type=REGULATORY_REFERENCE_TYPE,
            version=1,
            status='ACTIVE',
            rule_code='2210',
        ),
        Regulation(
            id='id-2211-2216',
            org_id='org_example',
            name='FINRA: Rules 2211–2216',
            type=REGULATORY_REFERENCE_TYPE,
            version=1,
            status='ACTIVE',
            rule_code='2211-2216',
        ),
    ]


class _FakeQuery:
    def __init__(self, session: _FakeSession, model: object) -> None:
        self.session = session
        self.model = model

    def filter(self, *_args: object, **_kwargs: object) -> _FakeQuery:
        known_ids = {row.id for row in self.session.rows}
        for expr in _args:
            right = getattr(expr, 'right', None)
            value = getattr(right, 'value', None)
            if isinstance(value, str) and value in known_ids:
                self.session.current_id = value
        return self

    def order_by(self, *_args: object, **_kwargs: object) -> _FakeQuery:
        return self

    def first(self) -> object | None:
        if self.model is DocumentChunk:
            return None
        return None

    def one_or_none(self) -> object | None:
        if self.model is not Regulation:
            return None
        by_id = {row.id: row for row in self.session.rows}
        return by_id.get(self.session.current_id)

    def all(self) -> list[object]:
        return []


class _FakeSession:
    def __init__(self, rows: list[Regulation]) -> None:
        self.rows = rows
        self.current_id: str | None = None
        self.added: list[object] = []

    def query(self, model: object) -> _FakeQuery:
        return _FakeQuery(self, model)

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_sync_marketing_regulation_documents_stores_docx_and_chunks(monkeypatch) -> None:
    rows = _catalog_rows()
    db = _FakeSession(rows)
    persisted: list[dict[str, object]] = []
    loaded_ids: list[tuple[str, ...]] = []

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield db

    async def fake_load(_settings: Settings, specs: tuple[object, ...] | None = None):
        selected = specs or RULE_SPECS
        loaded_ids.append(tuple(spec.rule_id for spec in selected))
        return [fallback_rule_document(spec) for spec in selected]

    async def fake_embed(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.11, 0.22] for _ in chunks]

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.ensure_marketing_regulations',
        lambda _db, _org_id: rows,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_marketing_rule_corpus',
        fake_load,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.generate_embeddings',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.store_reference_policy_file',
        lambda **kwargs: f'storage://marketing/{kwargs["policy_id"]}.docx',
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.delete_chunks_for_regulation_ids',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.persist_reference_chunks',
        lambda **kwargs: persisted.append(kwargs),
    )

    updated = await sync_marketing_regulation_documents('org_example', Settings())

    assert updated == 4
    assert loaded_ids == [
        ('206(4)-1', '204-2', '2210', '2211', '2212', '2213', '2214', '2215', '2216')
    ]
    assert [row.file_url for row in rows] == [
        'storage://marketing/id-206(4)-1.docx',
        'storage://marketing/id-204-2.docx',
        'storage://marketing/id-2210.docx',
        'storage://marketing/id-2211-2216.docx',
    ]
    assert len(persisted) == 4
    assert all(item['embeddings'] for item in persisted)
    assert {item['policy_id'] for item in persisted} == {row.id for row in rows}


@pytest.mark.asyncio
async def test_sync_marketing_regulation_documents_fetches_requested_rule_only(monkeypatch) -> None:
    rows = _catalog_rows()
    db = _FakeSession(rows)
    loaded_ids: list[tuple[str, ...]] = []

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield db

    async def fake_load(_settings: Settings, specs: tuple[object, ...] | None = None):
        selected = specs or RULE_SPECS
        loaded_ids.append(tuple(spec.rule_id for spec in selected))
        return [fallback_rule_document(spec) for spec in selected]

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.ensure_marketing_regulations',
        lambda _db, _org_id: rows,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_marketing_rule_corpus',
        fake_load,
    )
    async def fake_embed(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.1] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.generate_embeddings',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.store_reference_policy_file',
        lambda **kwargs: f'storage://marketing/{kwargs["policy_id"]}.docx',
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.delete_chunks_for_regulation_ids',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.persist_reference_chunks',
        lambda **_kwargs: None,
    )

    updated = await sync_marketing_regulation_documents(
        'org_example',
        Settings(),
        rule_codes={'2211-2216'},
    )

    assert updated == 1
    assert loaded_ids == [('2211', '2212', '2213', '2214', '2215', '2216')]
    assert rows[-1].file_url == 'storage://marketing/id-2211-2216.docx'
    assert rows[0].file_url is None


@pytest.mark.asyncio
async def test_sync_force_reembeds_when_content_hash_is_current(monkeypatch) -> None:
    rows = _catalog_rows()
    db = _FakeSession(rows)
    persisted: list[dict[str, object]] = []
    cache_cleared: list[str] = []

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield db

    async def fake_load(_settings: Settings, specs: tuple[object, ...] | None = None):
        selected = specs or RULE_SPECS
        return [fallback_rule_document(spec) for spec in selected]

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.ensure_marketing_regulations',
        lambda _db, _org_id: rows,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_marketing_rule_corpus',
        fake_load,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync._already_current',
        lambda *_args, **_kwargs: True,
    )
    async def fake_embed(
        chunks: list[object], _settings: Settings, **_kwargs: object
    ) -> list[list[float]]:
        return [[0.2] for _ in chunks]

    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.generate_embeddings',
        fake_embed,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.store_reference_policy_file',
        lambda **kwargs: f'storage://marketing/{kwargs["policy_id"]}.docx',
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.delete_chunks_for_regulation_ids',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.persist_reference_chunks',
        lambda **kwargs: persisted.append(kwargs),
    )
    monkeypatch.setattr(
        'app.services.marketing_rule_corpus.clear_rule_corpus_cache',
        lambda: cache_cleared.append('cleared'),
    )

    skipped = await sync_marketing_regulation_documents('org_example', Settings())
    forced = await sync_marketing_regulation_documents('org_example', Settings(), force=True)

    assert skipped == 0
    assert forced == 4
    assert cache_cleared == ['cleared']
    assert len(persisted) == 4
    assert all(item['embeddings'] for item in persisted)


@pytest.mark.asyncio
async def test_refresh_marketing_rules_skips_when_stored(monkeypatch) -> None:
    from contextlib import contextmanager

    called: list[object] = []

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield object()

    async def fake_sync(*_args: object, **_kwargs: object) -> int:
        called.append(_kwargs)
        return 4

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_stored_marketing_rule_documents',
        lambda *_args, **_kwargs: [object(), object(), object(), object()],
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.sync_marketing_regulation_documents',
        fake_sync,
    )

    from app.services.marketing_regulation_sync import refresh_marketing_rules_for_review

    updated = await refresh_marketing_rules_for_review('org_example', Settings())

    assert updated == 0
    assert called == []


@pytest.mark.asyncio
async def test_refresh_marketing_rules_syncs_when_missing(monkeypatch) -> None:
    from contextlib import contextmanager

    captured: dict[str, object] = {}

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield object()

    async def fake_sync(org_id: str, settings: object | None = None, **kwargs: object) -> int:
        captured['org_id'] = org_id
        captured['force'] = kwargs.get('force')
        return 4

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_stored_marketing_rule_documents',
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.sync_marketing_regulation_documents',
        fake_sync,
    )

    from app.services.marketing_regulation_sync import refresh_marketing_rules_for_review

    updated = await refresh_marketing_rules_for_review('org_example', Settings())

    assert updated == 4
    assert captured == {'org_id': 'org_example', 'force': False}


@pytest.mark.asyncio
async def test_refresh_marketing_rules_skips_when_already_running(monkeypatch) -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_sync(*_args: object, **_kwargs: object) -> int:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return 4

    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.sync_marketing_regulation_documents',
        slow_sync,
    )
    monkeypatch.setattr(
        'app.services.marketing_regulation_sync.load_stored_marketing_rule_documents',
        lambda *_args, **_kwargs: [],
    )

    from contextlib import contextmanager

    @contextmanager
    def fake_scope(**_kwargs: object):
        yield object()

    monkeypatch.setattr('app.db.session.session_scope', fake_scope)

    from app.services.marketing_regulation_sync import refresh_marketing_rules_for_review

    first = asyncio.create_task(refresh_marketing_rules_for_review('org_example', Settings()))
    await started.wait()
    skipped = await refresh_marketing_rules_for_review('org_example', Settings())
    release.set()
    assert await first == 4
    assert skipped == 0
    assert calls == 1
