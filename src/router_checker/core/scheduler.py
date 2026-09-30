"""When the next check is due."""

from __future__ import annotations

from datetime import datetime, timedelta

INTERVAL_CHOICES_MIN = (1, 5, 10, 15, 30)
DEFAULT_INTERVAL_MIN = 5
UNSTABLE_INTERVAL_MIN = 1


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
