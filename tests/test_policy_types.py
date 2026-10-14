from app.services.policy_types import (
    INTERNAL_POLICY_TYPE,
    REGULATORY_REFERENCE_TYPE,
    is_internal_policy_type,
    policy_type_label,
)


def test_policy_type_labels() -> None:
    assert policy_type_label(INTERNAL_POLICY_TYPE) == 'Internal'
    assert policy_type_label(REGULATORY_REFERENCE_TYPE) == 'Regulation'
    assert is_internal_policy_type(INTERNAL_POLICY_TYPE) is True
    assert is_internal_policy_type(REGULATORY_REFERENCE_TYPE) is False
    assert is_internal_policy_type('MARKETING_CONTENT') is False
