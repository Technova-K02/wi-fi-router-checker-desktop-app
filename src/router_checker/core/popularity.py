"""How busy a router is, and the hourly "popular times" grid.

Busyness is a value from 0 (idle) to 1 (very busy):

* With a BSS Load element: ``max(channel utilization, stations / 30)``.
* Without one (estimated): mean of ``same-channel networks / 8`` and
  ``recent jitter / 30 ms`` (each capped at 1; missing parts are skipped).

Levels: < 0.33 Low, < 0.66 Medium, otherwise High.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import tzinfo

from router_checker.core.models import BssLoad, BusyLevel, Busyness, HourlyAggregate

STATIONS_HIGH = 30
CROWDING_HIGH = 8
JITTER_BUSY_HIGH_MS = 30.0
LEVEL_MEDIUM = 0.33
LEVEL_HIGH = 0.66


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


def popular_times(
    aggregates: Iterable[HourlyAggregate], tz: tzinfo | None = None
) -> list[list[float | None]]:
    """7 x 24 grid (Monday first) of average busyness in local time.

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
    return [
        [sums[d][h] / counts[d][h] if counts[d][h] else None for h in range(24)] for d in range(7)
    ]
