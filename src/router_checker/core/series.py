"""Data for the history charts.

Times are epoch seconds (what the chart's date axis uses). A NaN value breaks
the line, so a pause in the checks (the PC slept, the app was closed, another
network was used) never shows up as a straight line across the gap.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from router_checker.core.models import CheckRecord, ScorePoint

MIN_GAP = timedelta(minutes=5)
GAP_FACTOR = 2.5  # a pause longer than 2.5 check intervals breaks the line
CHART_SPANS: tuple[tuple[str, timedelta], ...] = (
    ("1 h", timedelta(hours=1)),
    ("24 h", timedelta(hours=24)),
    ("7 d", timedelta(days=7)),
)


@dataclass(frozen=True, slots=True)
class Series:
    name: str
    xs: tuple[float, ...]  # epoch seconds
    ys: tuple[float, ...]  # NaN where there is no value

    @property
    def values(self) -> list[float]:
        return [y for y in self.ys if not math.isnan(y)]


def max_gap(interval_min: int) -> timedelta:
    """The longest pause between two points that is still drawn as a line."""
    return max(MIN_GAP, timedelta(minutes=interval_min * GAP_FACTOR))


def make_series(
    name: str, points: Iterable[tuple[datetime, float | None]], gap: timedelta
) -> Series:
    """``points`` in time order; ``None`` values and pauses over ``gap`` break the line."""
    xs: list[float] = []
    ys: list[float] = []
    previous: datetime | None = None
    for when, value in points:
        if previous is not None and when - previous > gap:
            xs.append((previous + (when - previous) / 2).timestamp())
            ys.append(math.nan)
        xs.append(when.timestamp())
        ys.append(math.nan if value is None else float(value))
        previous = when
    return Series(name, tuple(xs), tuple(ys))


@dataclass(frozen=True, slots=True)
class RouterCharts:
    """What the router details charts show for one time span."""

    router_id: str
    start: datetime
    end: datetime
    latency: tuple[Series, Series]  # gateway average, internet median (ms)
    loss: tuple[Series, Series]  # gateway, internet (%)
    score: tuple[Series, Series]  # measured, estimated
    checks: int  # full tests in the span


def router_charts(
    router_id: str,
    checks: Sequence[CheckRecord],
    scores: Sequence[ScorePoint],
    start: datetime,
    end: datetime,
    gap: timedelta,
) -> RouterCharts:
    rows = sorted((c for c in checks if start <= c.timestamp <= end), key=lambda c: c.timestamp)

    def series(name: str, value: Callable[[CheckRecord], float | None]) -> Series:
        return make_series(name, ((c.timestamp, value(c)) for c in rows), gap)

    def gateway(
        value: Callable[[CheckRecord], float | None],
    ) -> Callable[[CheckRecord], float | None]:
        # A gateway that ignores ping has no latency or loss of its own.
        return lambda c: None if c.gateway_silent else value(c)

    points = sorted((p for p in scores if start <= p.timestamp <= end), key=lambda p: p.timestamp)
    return RouterCharts(
        router_id=router_id,
        start=start,
        end=end,
        latency=(
            series("Gateway", gateway(lambda c: c.gateway_avg_ms)),
            series("Internet", lambda c: c.internet_latency_ms),
        ),
        loss=(
            series("Gateway", gateway(lambda c: c.gateway_loss_pct)),
            series("Internet", lambda c: c.internet_loss_pct),
        ),
        score=(
            make_series(
                "Score", ((p.timestamp, None if p.estimated else p.value) for p in points), gap
            ),
            make_series(
                "Estimated", ((p.timestamp, p.value if p.estimated else None) for p in points), gap
            ),
        ),
        checks=len(rows),
    )


@dataclass(frozen=True, slots=True)
class ScoreLine:
    """One router's score over time, for the comparison chart."""

    router_id: str
    measured: Series
    estimated: Series
    latest: ScorePoint | None


def score_lines(
    scores: Mapping[str, Sequence[ScorePoint]], start: datetime, end: datetime, gap: timedelta
) -> list[ScoreLine]:
    """One line per router that has scores in the span, in the order given."""
    lines = []
    for router_id, all_points in scores.items():
        points = sorted(
            (p for p in all_points if start <= p.timestamp <= end), key=lambda p: p.timestamp
        )
        if not points:
            continue
        lines.append(
            ScoreLine(
                router_id,
                make_series(
                    "Score",
                    ((p.timestamp, None if p.estimated else p.value) for p in points),
                    gap,
                ),
                make_series(
                    "Estimated",
                    ((p.timestamp, p.value if p.estimated else None) for p in points),
                    gap,
                ),
                points[-1],
            )
        )
    return lines
