from datetime import timedelta

from fakes import T0
from router_checker.core.models import Score
from router_checker.core.recommendation import recommend

NOW = T0


def s(value: int, age_min: float = 0, estimated: bool = False):
    return (Score(value, estimated), NOW - timedelta(minutes=age_min))


def test_recommends_when_better_by_ten() -> None:
    rec = recommend({"cur": s(60), "b": s(70)}, "cur", NOW)
    assert rec is not None and rec.router_id == "b" and rec.margin == 10


def test_not_when_margin_too_small() -> None:
    assert recommend({"cur": s(60), "b": s(69)}, "cur", NOW) is None


def test_not_when_current_is_best() -> None:
    assert recommend({"cur": s(90), "b": s(50)}, "cur", NOW) is None


def test_stale_data_ignored() -> None:
    assert recommend({"cur": s(50), "b": s(95, age_min=31)}, "cur", NOW) is None
    assert recommend({"cur": s(50), "b": s(95, age_min=30)}, "cur", NOW) is not None


def test_current_without_fresh_score_gets_no_recommendation() -> None:
    assert recommend({"cur": s(10, age_min=45), "b": s(95)}, "cur", NOW) is None


def test_not_connected_recommends_best() -> None:
    rec = recommend({"a": s(50), "b": s(80)}, None, NOW)
    assert rec is not None and rec.router_id == "b" and rec.margin is None


def test_tie_prefers_measured_over_estimated() -> None:
    rec = recommend({"a": s(80, estimated=True), "b": s(80)}, None, NOW)
    assert rec.router_id == "b"


def test_empty() -> None:
    assert recommend({}, None, NOW) is None
