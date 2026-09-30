import math
from datetime import timedelta

from fakes import T0, record
from router_checker.core.models import ScorePoint
from router_checker.core.presentation import fmt_ms, series_summary
from router_checker.core.series import (
    Series,
    make_series,
    max_gap,
    router_charts,
    score_lines,
)

GAP = timedelta(minutes=12, seconds=30)


def minutes(m: float):
    return T0 + timedelta(minutes=m)


def as_list(series: Series) -> list[tuple[float, float | None]]:
    return [(x, None if math.isnan(y) else y) for x, y in zip(series.xs, series.ys, strict=True)]


def test_max_gap_follows_the_interval() -> None:
    assert max_gap(5) == GAP
    assert max_gap(1) == timedelta(minutes=5)  # at least 5 minutes
    assert max_gap(30) == timedelta(minutes=75)


def test_pauses_and_missing_values_break_the_line() -> None:
    series = make_series("x", [(minutes(0), 1), (minutes(5), None), (minutes(10), 3),
                               (minutes(40), 4)], GAP)  # fmt: skip
    assert as_list(series) == [
        (minutes(0).timestamp(), 1.0),
        (minutes(5).timestamp(), None),
        (minutes(10).timestamp(), 3.0),
        (minutes(25).timestamp(), None),  # the 30-minute pause
        (minutes(40).timestamp(), 4.0),
    ]
    assert series.values == [1.0, 3.0, 4.0]


def test_router_charts() -> None:
    checks = [
        record(timestamp=minutes(10), gateway_avg_ms=3.0, internet_latency_ms=25.0),
        record(timestamp=minutes(0), gateway_avg_ms=2.0, internet_loss_pct=10.0),
        record(timestamp=minutes(5), gateway_silent=True, gateway_loss_pct=100.0),
        record(timestamp=minutes(-90)),  # before the span
    ]
    scores = [ScorePoint(minutes(0), 90, False), ScorePoint(minutes(5), 70, True)]
    charts = router_charts("r1", checks, scores, minutes(-60), minutes(15), GAP)
    assert charts.checks == 3
    gateway, internet = charts.latency
    assert gateway.values == [2.0, 3.0]  # the silent gateway has no latency of its own
    assert internet.values == [20.0, 20.0, 25.0]
    assert charts.loss[0].values == [0.0, 0.0] and charts.loss[1].values == [10.0, 0.0, 0.0]
    measured, estimated = charts.score
    assert measured.values == [90.0] and estimated.values == [70.0]
    assert len(measured.xs) == len(estimated.xs) == 2  # aligned, NaN where the other applies


def test_score_lines_skip_routers_without_scores_in_the_span() -> None:
    lines = score_lines(
        {
            "a": [ScorePoint(minutes(0), 80, False), ScorePoint(minutes(5), 82, False)],
            "b": [ScorePoint(minutes(-120), 50, True)],
        },
        minutes(-60),
        minutes(10),
        GAP,
    )
    assert [line.router_id for line in lines] == ["a"]
    assert lines[0].latest == ScorePoint(minutes(5), 82, False)
    assert lines[0].measured.values == [80.0, 82.0] and lines[0].estimated.values == []


def test_series_summary() -> None:
    series = make_series("Internet", [(minutes(i), v) for i, v in enumerate([20, 24, 120])], GAP)
    assert series_summary(series, fmt_ms) == "Internet: median 24 ms, highest 120 ms"
    score = make_series("Score", [(minutes(0), 90), (minutes(1), 40)], GAP)
    assert series_summary(score, str, lower_is_worse=True) == "Score: median 65.0, lowest 40.0"
    assert series_summary(make_series("Gateway", [], GAP), fmt_ms) == "Gateway: no data"
