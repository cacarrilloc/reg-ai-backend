from __future__ import annotations

from app.models.core import Regulation
from app.services.policy_types import REGULATORY_REFERENCE_TYPE
from app.services.regulation_catalog import (
    MARKETING_REGULATION_SEED,
    ensure_marketing_regulations,
    marketing_regulation_id,
)


def test_marketing_regulation_seed_contains_required_rules() -> None:
    names = {item['name'] for item in MARKETING_REGULATION_SEED}
    assert names == {
        'SEC: Rule 206(4)-1: The Marketing Rule',
        'SEC: Rule 204-2: Recordkeeping',
        'FINRA: Rule 2210: Communications with the public',
        'FINRA: Rules 2211–2216',
    }


def test_ensure_marketing_regulations_inserts_four_catalog_rows() -> None:
    class FakeQuery:
        def filter(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> FakeQuery:
            return self

        def first(self) -> None:
            return None

    class FakeSession:
        def __init__(self) -> None:
            self.added: list[object] = []

        def query(self, model: object) -> FakeQuery:
            del model
            return FakeQuery()

        def add(self, value: object) -> None:
            self.added.append(value)

        def flush(self) -> None:
            return None

    db = FakeSession()
    rows = ensure_marketing_regulations(db, 'org_example')  # type: ignore[arg-type]

    assert len(rows) == 4
    assert {row.rule_code for row in rows} == {'206(4)-1', '204-2', '2210', '2211-2216'}
    assert all(isinstance(row, Regulation) for row in rows)
    assert all(row.type == REGULATORY_REFERENCE_TYPE for row in rows)
    assert all(row.org_id == 'org_example' for row in rows)
    assert rows[0].id == marketing_regulation_id('org_example', '206(4)-1')


def test_catalog_rule_documents_groups_finra_2211_through_2216() -> None:
    from app.services.marketing_rule_corpus import (
        RULE_SPECS,
        RuleDocument,
        catalog_rule_documents,
        fallback_rule_document,
    )

    by_id = {spec.rule_id: spec for spec in RULE_SPECS}
    documents = [
        fallback_rule_document(by_id['206(4)-1']),
        fallback_rule_document(by_id['204-2']),
        fallback_rule_document(by_id['2210']),
        fallback_rule_document(by_id['2211']),
        fallback_rule_document(by_id['2212']),
        fallback_rule_document(by_id['2213']),
        fallback_rule_document(by_id['2214']),
        fallback_rule_document(by_id['2215']),
        fallback_rule_document(by_id['2216']),
    ]
    grouped = catalog_rule_documents(documents)
    assert [item.rule_id for item in grouped] == ['206(4)-1', '204-2', '2210', '2211-2216']
    combined = grouped[-1]
    assert isinstance(combined, RuleDocument)
    assert '2211' in combined.text
    assert '2216' in combined.text
    assert combined.title == 'FINRA: Rules 2211–2216'
