import pytest

from router_checker.core.stats import jitter, percentile, summarize


def test_percentile_linear_interpolation() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 95) == pytest.approx(9.55)
    assert percentile(values, 50) == pytest.approx(5.5)
    assert percentile([7], 95) == 7
    assert percentile([10, 0], 0) == 0
    with pytest.raises(ValueError):
        percentile([], 95)


def test_jitter_is_mean_abs_consecutive_difference() -> None:
    assert jitter([10, 20, 10, 10]) == pytest.approx((10 + 10 + 0) / 3)
    assert jitter([5]) is None


def test_summarize_with_losses() -> None:
    s = summarize([10.0, None, 20.0, 30.0, None])
    assert s.sent == 5
    assert s.received == 3
    assert s.loss_pct == pytest.approx(40.0)
    assert s.avg_ms == pytest.approx(20.0)
    assert s.median_ms == 20.0
    assert s.min_ms == 10.0 and s.max_ms == 30.0
    assert s.jitter_ms == pytest.approx(10.0)  # lost pings are skipped
    assert s.p95_ms == pytest.approx(29.0)
    assert not s.all_lost


def test_summarize_all_lost() -> None:
    s = summarize([None, None])
    assert s.all_lost
    assert s.loss_pct == 100.0
    assert s.avg_ms is None and s.median_ms is None and s.p95_ms is None and s.jitter_ms is None


def test_summarize_empty_counts_as_full_loss() -> None:
    assert summarize([]).loss_pct == 100.0
