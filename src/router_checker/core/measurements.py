"""Turning a full test into one flat, storable record.

Internet summary across the configured targets:

* loss = lost pings / sent pings over all targets (a target whose DNS lookup
  failed counts as ``pings_per_target`` lost pings)
* latency = median of the per-target median RTTs
* jitter = mean of the per-target jitters
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from router_checker.core.alerts import Thresholds, diagnose
from router_checker.core.models import CheckRecord, FullTestResult, TargetResult


@dataclass(frozen=True, slots=True)
class InternetSummary:
    loss_pct: float | None
    latency_ms: float | None
    jitter_ms: float | None
    dns_ms: float | None


def summarize_internet(targets: tuple[TargetResult, ...], pings_per_target: int) -> InternetSummary:
    if not targets:
        return InternetSummary(None, None, None, None)
    sent = lost = 0
    medians: list[float] = []
    jitters: list[float] = []
    for t in targets:
        if t.ping is None:
            sent += pings_per_target
            lost += pings_per_target
            continue
        sent += t.ping.sent
        lost += t.ping.sent - t.ping.received
        if t.ping.median_ms is not None:
            medians.append(t.ping.median_ms)
        if t.ping.jitter_ms is not None:
            jitters.append(t.ping.jitter_ms)
    dns_times = [t.dns.elapsed_ms for t in targets if t.dns is not None and t.dns.ok]
    return InternetSummary(
        loss_pct=100.0 * lost / sent if sent else None,
        latency_ms=statistics.median(medians) if medians else None,
        jitter_ms=statistics.fmean(jitters) if jitters else None,
        dns_ms=statistics.fmean(dns_times) if dns_times else None,
    )


def to_record(result: FullTestResult, thresholds: Thresholds, pings_per_target: int) -> CheckRecord:
    internet = summarize_internet(result.targets, pings_per_target)
    diagnosis = diagnose(
        result.gateway_ping, result.targets, internet.loss_pct, internet.jitter_ms, thresholds
    )
    gw = result.gateway_ping
    return CheckRecord(
        timestamp=result.timestamp,
        router_id=result.router_id,
        ssid=result.ssid,
        bssid=str(result.bssid) if result.bssid else None,
        verdict=diagnosis.verdict,
        reasons=diagnosis.reasons,
        gateway_loss_pct=gw.loss_pct if gw else None,
        gateway_avg_ms=gw.avg_ms if gw else None,
        gateway_p95_ms=gw.p95_ms if gw else None,
        gateway_jitter_ms=gw.jitter_ms if gw else None,
        gateway_silent=diagnosis.gateway_silent,
        internet_loss_pct=internet.loss_pct,
        internet_latency_ms=internet.latency_ms,
        internet_jitter_ms=internet.jitter_ms,
        dns_ms=internet.dns_ms,
        rssi=result.rssi,
        signal_quality=result.signal_quality,
    )
