"""Stability score (0-100).

Per-check score
---------------
Each factor is mapped linearly to 0..1 between a "best" and a "worst" value
(clamped), then combined with fixed weights::

    score = 100 * (0.40*loss + 0.25*jitter + 0.20*latency + 0.15*signal)

* loss:    0 %   -> 1,  >= 20 %   -> 0   (worst of gateway/internet loss)
* jitter:  0 ms  -> 1,  >= 50 ms  -> 0   (internet jitter, else gateway jitter)
* latency: 20 ms -> 1,  >= 200 ms -> 0   (median of the internet targets' medians)
* signal: -50 dBm -> 1, <= -85 dBm -> 0

A missing signal or jitter value is left out and the remaining weights are
rescaled. If every internet target failed, latency and jitter count as 0.
A silent gateway (ignores ping while the internet works) is left out of loss.

Rolling score
-------------
Weighted mean of per-check scores within the last 60 minutes. Weight halves
every 30 minutes of age: ``w = 0.5 ** (age_min / 30)``.

Estimated score (routers without a recent full test)
---------------------------------------------------
``0.6*signal + 0.4*(1 - busyness)`` (weights rescaled if one is missing),
averaged 50/50 with the router's historical score when there is one.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from router_checker.core.models import CheckRecord, Score

WEIGHT_LOSS = 0.40
WEIGHT_JITTER = 0.25
WEIGHT_LATENCY = 0.20
WEIGHT_SIGNAL = 0.15

LOSS_BEST_PCT, LOSS_WORST_PCT = 0.0, 20.0
JITTER_BEST_MS, JITTER_WORST_MS = 0.0, 50.0
LATENCY_BEST_MS, LATENCY_WORST_MS = 20.0, 200.0
RSSI_BEST_DBM, RSSI_WORST_DBM = -50.0, -85.0

WINDOW = timedelta(minutes=60)
HALF_LIFE = timedelta(minutes=30)

EST_WEIGHT_SIGNAL = 0.6
EST_WEIGHT_QUIET = 0.4
EST_HISTORY_SHARE = 0.5

LABEL_EXCELLENT = 80
LABEL_GOOD = 60
LABEL_FAIR = 40


def score_label(value: float) -> str:
    if value >= LABEL_EXCELLENT:
        return "Excellent"
    if value >= LABEL_GOOD:
        return "Good"
    if value >= LABEL_FAIR:
        return "Fair"
    return "Poor"


def linear(value: float, best: float, worst: float) -> float:
    """1.0 at ``best``, 0.0 at ``worst``, linear and clamped in between."""
    frac = (worst - value) / (worst - best)
    return max(0.0, min(1.0, frac))


def signal_factor(rssi: float) -> float:
    return linear(rssi, RSSI_BEST_DBM, RSSI_WORST_DBM)


def _weighted(parts: Iterable[tuple[float, float | None]]) -> float | None:
    present = [(w, f) for w, f in parts if f is not None]
    total = sum(w for w, _ in present)
    if total == 0:
        return None
    return sum(w * f for w, f in present) / total


def check_score(record: CheckRecord) -> float | None:
    """Score of a single check, or None for checks without a connection."""
    losses = [record.internet_loss_pct]
    if not record.gateway_silent:
        losses.append(record.gateway_loss_pct)
    known_losses = [x for x in losses if x is not None]
    if not known_losses:
        return None

    no_targets = record.internet_loss_pct is None
    if record.internet_latency_ms is None and not no_targets:  # every target failed
        latency_f: float | None = 0.0
        jitter_f: float | None = 0.0
    else:
        latency_f = (
            None
            if record.internet_latency_ms is None
            else linear(record.internet_latency_ms, LATENCY_BEST_MS, LATENCY_WORST_MS)
        )
        jitter = record.internet_jitter_ms
        if jitter is None:
            jitter = record.gateway_jitter_ms
        jitter_f = None if jitter is None else linear(jitter, JITTER_BEST_MS, JITTER_WORST_MS)

    value = _weighted(
        [
            (WEIGHT_LOSS, linear(max(known_losses), LOSS_BEST_PCT, LOSS_WORST_PCT)),
            (WEIGHT_JITTER, jitter_f),
            (WEIGHT_LATENCY, latency_f),
            (WEIGHT_SIGNAL, None if record.rssi is None else signal_factor(record.rssi)),
        ]
    )
    return None if value is None else 100.0 * value


def rolling_score(samples: Iterable[tuple[datetime, float]], now: datetime) -> float | None:
    """Recency-weighted mean of (timestamp, score) pairs inside the window."""
    num = den = 0.0
    for ts, value in samples:
        age = now - ts
        if age < timedelta(0) or age > WINDOW:
            continue
        weight = 0.5 ** (age / HALF_LIFE)
        num += weight * value
        den += weight
    return num / den if den else None


def estimated_score(
    rssi: float | None, busyness: float | None, history: float | None
) -> float | None:
    live = _weighted(
        [
            (EST_WEIGHT_SIGNAL, None if rssi is None else signal_factor(rssi)),
            (EST_WEIGHT_QUIET, None if busyness is None else 1.0 - busyness),
        ]
    )
    if live is None:
        return history
    live *= 100.0
    if history is None:
        return live
    return (1 - EST_HISTORY_SHARE) * live + EST_HISTORY_SHARE * history


def make_score(value: float | None, *, estimated: bool) -> Score | None:
    if value is None:
        return None
    return Score(round(max(0.0, min(100.0, value))), estimated)
