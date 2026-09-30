from datetime import UTC, datetime, timedelta, timezone

import pytest

from router_checker.core.models import BssLoad, BusyLevel, HourlyAggregate
from router_checker.core.popularity import BusyWindow, busy_level, busyness, popular_times


def test_levels() -> None:
    assert busy_level(0.0) is BusyLevel.LOW
    assert busy_level(0.33) is BusyLevel.MEDIUM
    assert busy_level(0.66) is BusyLevel.HIGH


def test_from_bss_load_uses_worse_of_utilization_and_stations() -> None:
    b = busyness(BssLoad(station_count=3, channel_utilization=51), same_channel=10)
    assert b.value == pytest.approx(0.2)  # 51/255, stations 3/30 = 0.1
    assert b.level is BusyLevel.LOW and not b.estimated
    b = busyness(BssLoad(station_count=24, channel_utilization=0), same_channel=0)
    assert b.value == pytest.approx(0.8)
    assert b.level is BusyLevel.HIGH


def test_estimated_from_crowding_and_jitter() -> None:
    b = busyness(None, same_channel=4, jitter_ms=15.0)
    assert b.value == pytest.approx(0.5)
    assert b.level is BusyLevel.MEDIUM and b.estimated
    assert busyness(None, same_channel=20).value == 1.0
    assert busyness(None, same_channel=None, jitter_ms=None) is None


def test_popular_times_grid() -> None:
    monday_9 = datetime(2026, 9, 28, 9, tzinfo=UTC)  # a Monday
    aggs = [
        HourlyAggregate("r", monday_9, 0.2, None),
        HourlyAggregate("r", monday_9 + timedelta(days=7), 0.4, None),
        HourlyAggregate("r", monday_9 + timedelta(hours=1), None, 80.0),
    ]
    times = popular_times(aggs, UTC)
    grid = times.values
    assert len(grid) == 7 and all(len(row) == 24 for row in grid)
    assert grid[0][9] == pytest.approx(0.3)
    assert times.counts[0][9] == 2 and times.hours_recorded == 2
    assert grid[0][10] is None  # a score but no busyness
    shifted = popular_times(aggs, timezone(timedelta(hours=-10)))  # 09:00 UTC Mon = 23:00 Sun
    assert shifted.values[6][23] == pytest.approx(0.3)


MONDAY = datetime(2026, 9, 28, tzinfo=UTC)


def a_week(busy_at: dict[tuple[int, int], float], other: float = 0.1) -> list[HourlyAggregate]:
    """One aggregate for every hour of a week; ``busy_at`` maps (day, hour) to a value."""
    return [
        HourlyAggregate("r", MONDAY + timedelta(days=d, hours=h), busy_at.get((d, h), other), None)
        for d in range(7)
        for h in range(24)
    ]


def test_busiest_needs_a_day_of_data() -> None:
    few = a_week({})[:23]
    assert popular_times(few, UTC).busiest() is None
    assert popular_times(a_week({})[:24], UTC).busiest() is not None


def test_busiest_spans_the_hours_close_to_the_peak() -> None:
    week = a_week({(1, 18): 0.62, (1, 19): 0.8, (1, 20): 0.75, (1, 21): 0.72, (1, 22): 0.5})
    assert popular_times(week, UTC).busiest() == BusyWindow(1, 19, 22, BusyLevel.HIGH)


def test_busiest_prefers_the_earliest_of_equal_peaks() -> None:
    week = a_week({(4, 9): 0.5, (2, 9): 0.5})
    assert popular_times(week, UTC).busiest() == BusyWindow(2, 9, 10, BusyLevel.MEDIUM)
