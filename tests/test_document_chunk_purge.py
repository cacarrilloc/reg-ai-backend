"""Tests for document chunk purge helpers."""

from __future__ import annotations

from app.services.document_chunk_service import delete_chunks_for_policy_ids


def test_delete_chunks_for_policy_ids_noop_on_empty() -> None:
    class Session:
        def query(self, *_args: object) -> object:
            raise AssertionError('empty policy id list should not query')

    assert delete_chunks_for_policy_ids(Session(), []) == 0  # type: ignore[arg-type]


def test_delete_chunks_for_policy_ids_returns_delete_count() -> None:
    class Query:
        def filter(self, *_args: object) -> Query:
            return self

        def delete(self, synchronize_session: bool = False) -> int:
            del synchronize_session
            return 3

    class Session:
        def query(self, *_args: object) -> Query:
            return Query()

    assert delete_chunks_for_policy_ids(Session(), ['p-old']) == 3  # type: ignore[arg-type]
