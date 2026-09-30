from datetime import timedelta

import pytest

from fakes import T0, record
from router_checker.core.models import Score
from router_checker.core.scoring import (
    check_score,
    estimated_score,
    linear,
    make_score,
    rolling_score,
    score_label,
    signal_factor,
)


def test_linear_both_directions_and_clamping() -> None:
    assert linear(20, 20, 200) == 1.0
    assert linear(110, 20, 200) == pytest.approx(0.5)
    assert linear(500, 20, 200) == 0.0
    assert linear(0, 20, 200) == 1.0
    assert signal_factor(-50) == 1.0
    assert signal_factor(-67.5) == pytest.approx(0.5)
    assert signal_factor(-95) == 0.0


def test_perfect_check_scores_100() -> None:
    assert check_score(record()) == pytest.approx(100.0)


def test_weights() -> None:
    # 20 % loss -> loss factor 0, everything else perfect -> 60
    assert check_score(record(internet_loss_pct=20.0)) == pytest.approx(60.0)
    # jitter 50 ms -> jitter factor 0 -> 75
    assert check_score(record(internet_jitter_ms=50.0)) == pytest.approx(75.0)
    # latency 200 ms -> 80
    assert check_score(record(internet_latency_ms=200.0)) == pytest.approx(80.0)
    # signal -85 dBm -> 85
    assert check_score(record(rssi=-85)) == pytest.approx(85.0)


def test_missing_signal_rescales_weights() -> None:
    value = check_score(record(rssi=None, internet_loss_pct=20.0))
    assert value == pytest.approx(100 * (0.25 + 0.20) / 0.85)


def test_all_targets_failed_scores_low() -> None:
    r = record(internet_loss_pct=100.0, internet_latency_ms=None, internet_jitter_ms=None)
    assert check_score(r) == pytest.approx(15.0)  # only the signal part is left


def test_silent_gateway_loss_ignored() -> None:
    assert check_score(record(gateway_loss_pct=100.0, gateway_silent=True)) == pytest.approx(100.0)
    assert check_score(record(gateway_loss_pct=100.0)) < 61


def test_no_data_gives_none() -> None:
    assert check_score(record(gateway_loss_pct=None, internet_loss_pct=None)) is None


def test_rolling_score_prefers_recent() -> None:
    now = T0 + timedelta(minutes=60)
    samples = [(T0 + timedelta(minutes=30), 40.0), (now, 100.0)]
    # weights: 0.5 (30 min old) and 1.0
    assert rolling_score(samples, now) == pytest.approx((0.5 * 40 + 100) / 1.5)


def test_rolling_score_window() -> None:
    now = T0 + timedelta(minutes=90)
    assert rolling_score([(T0, 10.0)], now) is None
    assert rolling_score([(now + timedelta(minutes=1), 10.0)], now) is None
    assert rolling_score([], now) is None


def test_estimated_score() -> None:
    assert estimated_score(-50, 0.0, None) == pytest.approx(100.0)
    assert estimated_score(-85, 1.0, None) == pytest.approx(0.0)
    assert estimated_score(-50, None, None) == pytest.approx(100.0)
    assert estimated_score(-50, 0.0, 60.0) == pytest.approx(80.0)
    assert estimated_score(None, None, 55.0) == 55.0
    assert estimated_score(None, None, None) is None


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (100, "Excellent"),
        (80, "Excellent"),
        (79, "Good"),
        (60, "Good"),
        (59, "Fair"),
        (40, "Fair"),
        (39, "Poor"),
        (0, "Poor"),
    ],
)
def test_labels(value: int, label: str) -> None:
    assert score_label(value) == label
    assert Score(value, False).label == label


def test_make_score_rounds_and_clamps() -> None:
    assert make_score(79.6, estimated=False) == Score(80, False)
    assert make_score(120, estimated=True) == Score(100, True)
    assert make_score(None, estimated=False) is None
