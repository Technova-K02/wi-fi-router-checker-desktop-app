"""Ping statistics: average, median, p95, jitter and packet loss."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from itertools import pairwise

from router_checker.core.models import PingStats


def percentile(values: Sequence[float], pct: float) -> float:
    """Percentile with linear interpolation between closest ranks.

    Same as numpy's default ("linear") method: position = pct/100 * (n - 1).
    """
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= pct <= 100:
        raise ValueError("pct must be between 0 and 100")
    ordered = sorted(values)
    pos = pct / 100 * (len(ordered) - 1)
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (pos - lower)


def jitter(rtts: Sequence[float]) -> float | None:
    """Mean absolute difference between consecutive RTTs (None if fewer than 2)."""
    if len(rtts) < 2:
        return None
    return sum(abs(b - a) for a, b in pairwise(rtts)) / (len(rtts) - 1)


def summarize(samples: Sequence[float | None]) -> PingStats:
    """Summarize a ping series; ``None`` entries are lost packets.

    Jitter uses only the answered pings, in the order they were sent.
    """
    rtts = [s for s in samples if s is not None]
    sent = len(samples)
    received = len(rtts)
    loss = 100.0 * (sent - received) / sent if sent else 100.0
    return PingStats(
        samples=tuple(samples),
        sent=sent,
        received=received,
        loss_pct=loss,
        avg_ms=statistics.fmean(rtts) if rtts else None,
        median_ms=statistics.median(rtts) if rtts else None,
        p95_ms=percentile(rtts, 95) if rtts else None,
        min_ms=min(rtts) if rtts else None,
        max_ms=max(rtts) if rtts else None,
        jitter_ms=jitter(rtts),
    )
