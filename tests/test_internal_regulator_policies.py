"""Tests for internal regulator policy naming and scoped government retrieval."""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.config import Settings
from app.services.policy_comparison_service import (
    REGULATOR_SOURCES,
    retrieve_government_excerpts_for_internal_policy,
)
from app.services.policy_types import (
    INTERNAL_REGULATOR_POLICY_OPTIONS,
    resolve_internal_regulator_source,
    validate_internal_regulator_policy_name,
)
from app.services.regulatory_reference_policy_service import reference_policy_name


def test_validate_internal_regulator_policy_name_accepts_free_text() -> None:
    assert validate_internal_regulator_policy_name("finra") == "finra"
    assert validate_internal_regulator_policy_name(" SEC ") == "SEC"
    assert validate_internal_regulator_policy_name("CFTC") == "CFTC"
    assert validate_internal_regulator_policy_name("AML Policy") == "AML Policy"


def test_validate_internal_regulator_policy_name_rejects_empty() -> None:
    with pytest.raises(HTTPException) as exc_info:
        validate_internal_regulator_policy_name("   ")
    assert exc_info.value.status_code == 422


def test_resolve_internal_regulator_source() -> None:
    assert resolve_internal_regulator_source("FINRA") == "FINRA"
    assert resolve_internal_regulator_source("FINRA Regulations") == "FINRA"
    assert resolve_internal_regulator_source("FINRA Latest Regulations") == "FINRA"
    assert resolve_internal_regulator_source("SEC Newsroom Reference") == "SEC"
    assert resolve_internal_regulator_source("CFTC Regulations") == "CFTC"
    assert resolve_internal_regulator_source("Electronic Communications") is None


def test_internal_regulator_policy_options_cover_all_sources() -> None:
    assert set(INTERNAL_REGULATOR_POLICY_OPTIONS) == {"FINRA", "SEC", "CFTC"}


def test_reference_policy_name_mapping_matches_policy_table_rows() -> None:
    assert reference_policy_name("FINRA") == "FINRA Regulations"
    assert reference_policy_name("SEC") == "SEC Regulations"
    assert reference_policy_name("CFTC") == "CFTC Regulations"


@pytest.mark.asyncio
async def test_retrieve_government_excerpts_for_internal_policy_includes_all_regulators(
    monkeypatch,
) -> None:
    captured_calls: list[dict[str, object]] = []

    async def fake_retrieve_for_text(
        _db, _org_id, _query, _settings, *, limit, policy_ids=None, **_kwargs
    ):
        source = "FINRA"
        if policy_ids == ["gov-sec"]:
            source = "SEC"
        elif policy_ids == ["gov-cftc"]:
            source = "CFTC"
        elif policy_ids == ["gov-finra"]:
            source = "FINRA"
        captured_calls.append({"limit": limit, "policy_ids": policy_ids, "source": source})
        return [
            SimpleNamespace(
                policy_id=f"gov-{source.lower()}",
                policy_name=f"{source} Regulations",
                regulator_source=source,
                chunk_id=f"chunk-{source.lower()}",
                chunk_index=0,
                content=f"{source} notice excerpt.",
                similarity=0.91,
                as_prompt_dict=lambda source=source: {"regulator_source": source},
            )
        ]

    def fake_active_policy(_db, _org_id, source: str):
        return SimpleNamespace(id=f"gov-{source.lower()}", name=f"{source} Regulations")

    class _FakeDb:
        def close(self) -> None:
            return None

        def rollback(self) -> None:
            return None

        def commit(self) -> None:
            return None

    monkeypatch.setattr(
        "app.services.policy_comparison_service.optional_embedding_provider",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.policy_comparison_service.retrieve_government_excerpts_for_text",
        fake_retrieve_for_text,
    )
    monkeypatch.setattr(
        "app.services.policy_comparison_service.active_reference_policy_for_regulator",
        fake_active_policy,
    )
    monkeypatch.setattr(
        "app.services.policy_comparison_service.merge_government_excerpts",
        lambda excerpts: list(excerpts),
    )
    monkeypatch.setattr(
        "app.services.policy_comparison_service.SessionLocal",
        lambda: _FakeDb(),
    )

    excerpts = await retrieve_government_excerpts_for_internal_policy(
        db=None,  # type: ignore[arg-type]
        org_id="org_example",
        internal_policy_name="FINRA",
        internal_policy_text="Internal FINRA supervisory procedures.",
        trigger_text="Policy upload FINRA v2",
        settings=Settings(),
    )

    assert len(captured_calls) == len(REGULATOR_SOURCES)
    assert all(call["policy_ids"] is not None for call in captured_calls)
    assert {excerpt.regulator_source for excerpt in excerpts} == set(REGULATOR_SOURCES)
