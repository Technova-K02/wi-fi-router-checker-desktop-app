"""When the next check, and the next scheduled Test all, are due."""

from __future__ import annotations

from datetime import datetime, timedelta

INTERVAL_CHOICES_MIN = (1, 5, 10, 15, 30)
DEFAULT_INTERVAL_MIN = 5
UNSTABLE_INTERVAL_MIN = 1

TEST_ALL_INTERVAL_CHOICES_H = (1, 2, 4, 8)
DEFAULT_TEST_ALL_INTERVAL_H = 2
TEST_ALL_MIN_IDLE = timedelta(minutes=5)  # no keyboard or mouse input for this long


def effective_interval(interval_min: int, unstable: bool) -> timedelta:
    """The normal interval, shortened to 1 minute while the router is unstable."""
    minutes = min(interval_min, UNSTABLE_INTERVAL_MIN) if unstable else interval_min
    return timedelta(minutes=minutes)


def next_check_at(
    last_check: datetime | None, now: datetime, interval_min: int, unstable: bool
) -> datetime:
    if last_check is None:
        return now
    return last_check + effective_interval(interval_min, unstable)


def seconds_until(due: datetime, now: datetime) -> float:
    return max(0.0, (due - now).total_seconds())


def scheduled_test_all_due(
    last_run: datetime | None, now: datetime, interval_h: int, idle: timedelta | None
) -> bool:
    """A scheduled Test all is due once ``interval_h`` hours have passed since the last
    one (or there was none), and only while the user has been idle for 5 minutes.
    ``idle`` is None when it can't be measured: then it never runs."""
    if idle is None or idle < TEST_ALL_MIN_IDLE:
        return False
    return last_run is None or now - last_run >= timedelta(hours=interval_h)
