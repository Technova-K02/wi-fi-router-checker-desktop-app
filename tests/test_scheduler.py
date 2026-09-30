from datetime import timedelta

from fakes import T0
from router_checker.core.scheduler import (
    effective_interval,
    next_check_at,
    scheduled_test_all_due,
    seconds_until,
)


def test_effective_interval() -> None:
    assert effective_interval(5, unstable=False) == timedelta(minutes=5)
    assert effective_interval(30, unstable=True) == timedelta(minutes=1)
    assert effective_interval(1, unstable=True) == timedelta(minutes=1)


def test_next_check() -> None:
    assert next_check_at(None, T0, 5, False) == T0
    assert next_check_at(T0, T0, 15, False) == T0 + timedelta(minutes=15)
    assert next_check_at(T0, T0, 15, True) == T0 + timedelta(minutes=1)


def test_seconds_until() -> None:
    assert seconds_until(T0 + timedelta(seconds=30), T0) == 30
    assert seconds_until(T0, T0 + timedelta(seconds=5)) == 0


def test_scheduled_test_all_waits_for_the_interval_and_for_idle() -> None:
    away = timedelta(minutes=5)
    assert scheduled_test_all_due(None, T0, 2, away)  # never ran
    assert not scheduled_test_all_due(None, T0, 2, timedelta(minutes=4, seconds=59))
    assert not scheduled_test_all_due(None, T0, 2, None)  # idle time unknown
    last = T0 - timedelta(hours=2)
    assert scheduled_test_all_due(last, T0, 2, away)
    assert not scheduled_test_all_due(last, T0, 4, away)
    assert not scheduled_test_all_due(T0 - timedelta(minutes=119), T0, 2, timedelta(hours=1))
