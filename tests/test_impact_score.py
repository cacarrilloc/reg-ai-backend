from app.core.impact_score import is_informational_impact, normalize_impact_score


def test_normalize_impact_score_maps_aliases() -> None:
    assert normalize_impact_score('NONE') == 'INFO'
    assert normalize_impact_score('info') == 'INFO'
    assert normalize_impact_score('LOW') == 'INFO'
    assert normalize_impact_score('CRITICAL') == 'HIGH'


def test_is_informational_impact() -> None:
    assert is_informational_impact('INFO') is True
    assert is_informational_impact('HIGH') is False
