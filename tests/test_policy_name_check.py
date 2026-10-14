import pytest

from app.models.core import Policy
from app.services.policy_service import check_policy_name, resolve_canonical_policy_name
from app.services.policy_types import INTERNAL_POLICY_TYPE, REGULATORY_REFERENCE_TYPE


class PolicyQuery:
    def __init__(self, policies: list[Policy]) -> None:
        self.policies = policies

    def filter(self, *_args: object, **_kwargs: object) -> "PolicyQuery":
        return self

    def order_by(self, *_args: object, **_kwargs: object) -> "PolicyQuery":
        return self

    def all(self) -> list[Policy]:
        return self.policies

    def first(self) -> Policy | None:
        return self.policies[0] if self.policies else None


class PolicySession:
    def __init__(self, policies: list[Policy]) -> None:
        self.policies = policies

    def query(self, *_args: object) -> PolicyQuery:
        return PolicyQuery(self.policies)


def policy(policy_id: str, name: str, version: int, policy_type: str = INTERNAL_POLICY_TYPE) -> Policy:
    return Policy(
        id=policy_id,
        org_id="org_example",
        name=name,
        type=policy_type,
        version=version,
        status="ACTIVE",
    )


@pytest.mark.asyncio
async def test_check_policy_name_finds_case_insensitive_existing_policy() -> None:
    db = PolicySession(
        [
            policy("wsp-v2", "Written Supervisory Procedures", 2),
            policy("wsp-v1", "Written Supervisory Procedures", 1),
        ]
    )

    result = await check_policy_name(
        "org_example",
        "written supervisory procedures",
        db,  # type: ignore[arg-type]
    )

    assert result.exact_match is not None
    assert result.exact_match.name == "Written Supervisory Procedures"
    assert result.exact_match.version == 2
    assert result.exact_match.version_count == 2
    assert result.similar_names == []


@pytest.mark.asyncio
async def test_check_policy_name_suggests_close_internal_names_only() -> None:
    db = PolicySession(
        [
            policy("wsp", "Written Supervisory Procedures", 3),
            policy("aml", "Anti-Money Laundering Policy", 1),
            policy("finra-ref", "FINRA Regulations", 1, REGULATORY_REFERENCE_TYPE),
        ]
    )

    result = await check_policy_name(
        "org_example",
        "Written Supervisory Procedure",
        db,  # type: ignore[arg-type]
    )

    assert result.exact_match is None
    assert [item.name for item in result.similar_names] == ["Written Supervisory Procedures"]


def test_resolve_canonical_policy_name_reuses_existing_display_name() -> None:
    db = PolicySession([policy("aml", "AML Policy", 2)])

    assert (
        resolve_canonical_policy_name(
            db,  # type: ignore[arg-type]
            "org_example",
            "aml policy",
        )
        == "AML Policy"
    )
