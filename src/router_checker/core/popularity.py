"""How busy a router is, and the hourly "popular times" grid.

Busyness is a value from 0 (idle) to 1 (very busy):

* With a BSS Load element: ``max(channel utilization, stations / 30)``.
* Without one (estimated): mean of ``same-channel networks / 8`` and
  ``recent jitter / 30 ms`` (each capped at 1; missing parts are skipped).

Levels: < 0.33 Low, < 0.66 Medium, otherwise High.

Popular times: the hourly averages from history, grouped by local weekday and
hour (7 x 24), like the "popular times" of a shop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import tzinfo

from router_checker.core.models import BssLoad, BusyLevel, Busyness, HourlyAggregate

STATIONS_HIGH = 30
CROWDING_HIGH = 8
JITTER_BUSY_HIGH_MS = 30.0
LEVEL_MEDIUM = 0.33
LEVEL_HIGH = 0.66
MIN_HOURS_FOR_SUMMARY = 24  # recorded hours before "usually busiest ..." is shown
BUSIEST_TOLERANCE = 0.1  # neighbouring hours this close to the peak count as busiest


def busy_level(value: float) -> BusyLevel:
    if value >= LEVEL_HIGH:
        return BusyLevel.HIGH
    if value >= LEVEL_MEDIUM:
        return BusyLevel.MEDIUM
    return BusyLevel.LOW


def busyness(
    bss_load: BssLoad | None, same_channel: int | None, jitter_ms: float | None = None
) -> Busyness | None:
    if bss_load is not None:
        value = max(
            bss_load.channel_utilization / 255.0, min(bss_load.station_count / STATIONS_HIGH, 1.0)
        )
        return Busyness(value, busy_level(value), estimated=False)
    parts = []
    if same_channel is not None:
        parts.append(min(same_channel / CROWDING_HIGH, 1.0))
    if jitter_ms is not None:
        parts.append(min(jitter_ms / JITTER_BUSY_HIGH_MS, 1.0))
    if not parts:
        return None
    value = sum(parts) / len(parts)
    return Busyness(value, busy_level(value), estimated=True)


@dataclass(frozen=True, slots=True)
class BusyWindow:
    """The busiest stretch of hours on one weekday."""

    day: int  # 0 = Monday
    start_hour: int
    end_hour: int  # exclusive, so 19-22 covers 19:00 to 22:00
    level: BusyLevel


@dataclass(frozen=True, slots=True)
class PopularTimes:
    """Average busyness by local weekday (Monday first) and hour, ``None`` where
    there is no data, and how many recorded hours each cell averages."""

    values: tuple[tuple[float | None, ...], ...]  # 7 x 24
    counts: tuple[tuple[int, ...], ...]  # 7 x 24

    @property
    def hours_recorded(self) -> int:
        return sum(map(sum, self.counts))

    def busiest(self) -> BusyWindow | None:
        """The busiest hour and the neighbouring hours on the same day that are
        almost as busy (within ``BUSIEST_TOLERANCE``). None until there are
        ``MIN_HOURS_FOR_SUMMARY`` recorded hours."""
        if self.hours_recorded < MIN_HOURS_FOR_SUMMARY:
            return None
        cells = [
            (value, day, hour)
            for day, row in enumerate(self.values)
            for hour, value in enumerate(row)
            if value is not None
        ]
        top, day, hour = max(cells, key=lambda c: (c[0], -c[1], -c[2]))
        row = self.values[day]

        def close(h: int) -> bool:
            value = row[h]
            return value is not None and value >= top - BUSIEST_TOLERANCE

        start, end = hour, hour + 1
        while start > 0 and close(start - 1):
            start -= 1
        while end < 24 and close(end):
            end += 1
        return BusyWindow(day, start, end, busy_level(top))


def popular_times(aggregates: Iterable[HourlyAggregate], tz: tzinfo | None = None) -> PopularTimes:
    """Average the hourly busyness by local weekday and hour.

    ``tz=None`` uses the computer's local time zone.
    """
    sums = [[0.0] * 24 for _ in range(7)]
    counts = [[0] * 24 for _ in range(7)]
    for agg in aggregates:
        if agg.busyness_avg is None:
            continue
        local = agg.hour_start.astimezone(tz)
        sums[local.weekday()][local.hour] += agg.busyness_avg
        counts[local.weekday()][local.hour] += 1
    values = tuple(
        tuple(sums[d][h] / counts[d][h] if counts[d][h] else None for h in range(24))
        for d in range(7)
    )
    return PopularTimes(values, tuple(tuple(row) for row in counts))
