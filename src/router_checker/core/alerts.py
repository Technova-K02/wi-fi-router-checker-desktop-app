"""Instability rules and the alert state machine.

A check is *unstable* when any of these holds (thresholds editable in Settings):

* packet loss >= ``loss_pct`` (gateway or internet, whichever is worse)
* gateway p95 > ``gateway_p95_ms``
* jitter > ``jitter_ms`` (gateway or internet, whichever is worse)
* all internet targets fail

Some routers ignore ping. When the gateway never answers but the internet
targets do, the gateway is "silent": its loss and latency are ignored.

Alerts: notify after ``unstable_checks`` unstable checks in a row, then stay
quiet for ``cooldown`` per router. Send "back to normal" after
``recovery_checks`` stable checks in a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from router_checker.core.models import (
    Alert,
    AlertKind,
    InstabilityReason,
    PingStats,
    TargetResult,
    Verdict,
)

DEFAULT_LOSS_PCT = 5.0
DEFAULT_GATEWAY_P95_MS = 100.0
DEFAULT_JITTER_MS = 30.0
DEFAULT_UNSTABLE_CHECKS = 2
DEFAULT_RECOVERY_CHECKS = 2
DEFAULT_COOLDOWN_MIN = 15


@dataclass(frozen=True, slots=True)
class Thresholds:
    loss_pct: float = DEFAULT_LOSS_PCT
    gateway_p95_ms: float = DEFAULT_GATEWAY_P95_MS
    jitter_ms: float = DEFAULT_JITTER_MS

    def __post_init__(self) -> None:
        if not 0 < self.loss_pct <= 100:
            raise ValueError("loss threshold must be in (0, 100]")
        if self.gateway_p95_ms <= 0 or self.jitter_ms <= 0:
            raise ValueError("latency and jitter thresholds must be positive")


@dataclass(frozen=True, slots=True)
class Diagnosis:
    verdict: Verdict
    reasons: tuple[InstabilityReason, ...]
    gateway_silent: bool


def diagnose(
    gateway: PingStats | None,
    targets: tuple[TargetResult, ...],
    internet_loss_pct: float | None,
    internet_jitter_ms: float | None,
    thresholds: Thresholds,
) -> Diagnosis:
    if gateway is None:
        return Diagnosis(Verdict.NOT_CONNECTED, (), False)

    all_targets_failed = bool(targets) and all(t.failed for t in targets)
    gateway_silent = gateway.all_lost and bool(targets) and not all_targets_failed

    if gateway.all_lost and not gateway_silent:
        return Diagnosis(
            Verdict.ROUTER_UNREACHABLE, (InstabilityReason.GATEWAY_UNREACHABLE,), False
        )

    reasons: list[InstabilityReason] = []
    if all_targets_failed:
        reasons.append(InstabilityReason.ALL_TARGETS_FAILED)

    losses = [] if gateway_silent else [gateway.loss_pct]
    if internet_loss_pct is not None and not all_targets_failed:
        losses.append(internet_loss_pct)
    if losses and max(losses) >= thresholds.loss_pct:
        reasons.append(InstabilityReason.HIGH_LOSS)

    gateway_p95 = None if gateway_silent else gateway.p95_ms
    if gateway_p95 is not None and gateway_p95 > thresholds.gateway_p95_ms:
        reasons.append(InstabilityReason.HIGH_GATEWAY_LATENCY)

    jitters = [j for j in (gateway.jitter_ms, internet_jitter_ms) if j is not None]
    if jitters and max(jitters) > thresholds.jitter_ms:
        reasons.append(InstabilityReason.HIGH_JITTER)

    if all_targets_failed:
        verdict = Verdict.INTERNET_DOWN
    elif reasons:
        verdict = Verdict.UNSTABLE
    else:
        verdict = Verdict.OK
    return Diagnosis(verdict, tuple(reasons), gateway_silent)


@dataclass(frozen=True, slots=True)
class AlertSnapshot:
    bad_streak: int
    good_streak: int
    alerting: bool  # an "unstable" alert went out and "back to normal" has not yet


@dataclass(slots=True)
class _RouterAlertState:
    bad_streak: int = 0
    good_streak: int = 0
    alerting: bool = False
    last_alert_at: datetime | None = None


@dataclass(slots=True)
class AlertEngine:
    unstable_checks: int = DEFAULT_UNSTABLE_CHECKS
    recovery_checks: int = DEFAULT_RECOVERY_CHECKS
    cooldown: timedelta = timedelta(minutes=DEFAULT_COOLDOWN_MIN)
    _states: dict[str, _RouterAlertState] = field(default_factory=dict)

    def is_unstable(self, router_id: str) -> bool:
        """True while the last check was unstable or recovery is not confirmed yet."""
        state = self._states.get(router_id)
        return state is not None and (state.bad_streak > 0 or state.alerting)

    def snapshot(self, router_id: str) -> AlertSnapshot:
        state = self._states.get(router_id)
        if state is None:
            return AlertSnapshot(0, 0, False)
        return AlertSnapshot(state.bad_streak, state.good_streak, state.alerting)

    def process(
        self,
        router_id: str,
        router_name: str,
        now: datetime,
        verdict: Verdict,
        reasons: tuple[InstabilityReason, ...],
    ) -> Alert | None:
        state = self._states.setdefault(router_id, _RouterAlertState())
        unstable = verdict is not Verdict.OK
        if unstable:
            state.bad_streak += 1
            state.good_streak = 0
            cooled_down = state.last_alert_at is None or now - state.last_alert_at >= self.cooldown
            if state.bad_streak >= self.unstable_checks and cooled_down:
                state.alerting = True
                state.last_alert_at = now
                return Alert(AlertKind.UNSTABLE, router_id, router_name, now, verdict, reasons)
            return None

        state.good_streak += 1
        state.bad_streak = 0
        if state.alerting and state.good_streak >= self.recovery_checks:
            state.alerting = False
            return Alert(AlertKind.RECOVERED, router_id, router_name, now, verdict)
        return None
