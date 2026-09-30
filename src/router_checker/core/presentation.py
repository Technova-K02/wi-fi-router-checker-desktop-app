"""What the app shows: status levels, texts and number formatting.

Pure Python so it is unit-tested on any OS; the UI only renders these.

Status levels (tray icon and dashboard):

* GOOD (green, check mark): the last check was OK and the score is 60 or more.
* WARNING (yellow, "!"): one unstable check that is not confirmed yet,
  recovering after an alert, or a score under 60.
* BAD (red, cross): unstable for ``unstable_checks`` checks in a row, router
  not responding, or an internet provider problem.
* UNKNOWN (grey, "?"): not connected, unknown network, no data yet, or the
  check itself failed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum

from router_checker.core.checker import CycleReport
from router_checker.core.mac import MacAddress
from router_checker.core.models import (
    Band,
    BusyLevel,
    Busyness,
    InstabilityReason,
    Recommendation,
    Router,
    RouterState,
    ScanEntry,
    Score,
    Verdict,
)
from router_checker.core.popularity import PopularTimes, busy_level
from router_checker.core.quiet_hours import QuietHours, format_hhmm
from router_checker.core.scoring import LABEL_GOOD

DASH = "\N{EN DASH}"
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TOOLTIP_MAX = 127  # Windows truncates tray tooltips at 128 characters


class StatusLevel(StrEnum):
    GOOD = "good"
    WARNING = "warning"
    BAD = "bad"
    UNKNOWN = "unknown"

    @property
    def glyph(self) -> str:
        return _GLYPHS[self]


_GLYPHS = {
    StatusLevel.GOOD: "✓",
    StatusLevel.WARNING: "!",
    StatusLevel.BAD: "✕",
    StatusLevel.UNKNOWN: "?",
}


@dataclass(frozen=True, slots=True)
class Status:
    level: StatusLevel
    title: str
    detail: str = ""

    @property
    def text(self) -> str:
        return f"{self.title}: {self.detail}" if self.detail else self.title


def overall_status(report: CycleReport | None, failure: str | None = None) -> Status:
    """Status of the connection right now (tray icon, flyout, dashboard)."""
    if failure is not None:
        return Status(StatusLevel.UNKNOWN, "Check failed", failure)
    if report is None:
        return Status(StatusLevel.UNKNOWN, "Waiting for the first check")
    record = report.record
    if report.gateway is None or record is None:
        return Status(StatusLevel.UNKNOWN, "Not connected", "The Wi-Fi adapter has no gateway.")
    if report.match.router is None:
        ssid = report.connection.ssid if report.connection else ""
        name = f'"{ssid}"' if ssid else "This network"
        return Status(StatusLevel.UNKNOWN, "Unknown network", f"{name} is not one of your routers.")

    verdict = record.verdict
    if verdict is Verdict.ROUTER_UNREACHABLE:
        return Status(
            StatusLevel.BAD,
            "Router not responding",
            "Neither the router nor the internet answered.",
        )
    if verdict is Verdict.INTERNET_DOWN:
        return Status(
            StatusLevel.BAD,
            "Internet provider problem",
            "The router answers, but no internet target does.",
        )
    if verdict is Verdict.UNSTABLE:
        reasons = reasons_text(record.reasons)
        if report.confirmed_unstable:
            return Status(StatusLevel.BAD, "Unstable", reasons)
        return Status(StatusLevel.WARNING, "Possibly unstable", reasons)
    if report.recovering:
        return Status(StatusLevel.WARNING, "Recovering", "Stable again, confirming.")
    current = report.current
    if current is not None and current.score is not None and current.score.value < LABEL_GOOD:
        return Status(
            StatusLevel.WARNING,
            current.score.label,
            f"Stability score {current.score.value} of 100.",
        )
    return Status(StatusLevel.GOOD, "Stable", "All checks passed.")


def verdict_level(verdict: Verdict) -> StatusLevel:
    """Level of a single stored check (History), without the streak context."""
    return {
        Verdict.OK: StatusLevel.GOOD,
        Verdict.UNSTABLE: StatusLevel.WARNING,
        Verdict.INTERNET_DOWN: StatusLevel.BAD,
        Verdict.ROUTER_UNREACHABLE: StatusLevel.BAD,
        Verdict.NOT_CONNECTED: StatusLevel.UNKNOWN,
    }[verdict]


# --- numbers ---------------------------------------------------------------------


def fmt_ms(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{value:.1f} ms" if value < 10 else f"{value:.0f} ms"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{value:.1f}%" if 0 < value < 1 else f"{value:.0f}%"


def fmt_dbm(value: int | None) -> str:
    return DASH if value is None else f"{value} dBm"


def fmt_countdown(seconds: float) -> str:
    total = max(0, math.ceil(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def fmt_age(then: datetime, now: datetime) -> str:
    seconds = (now - then).total_seconds()
    if seconds < 45:
        return "just now"
    if seconds < 90 * 60:
        return f"{max(1, round(seconds / 60))} min ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} d ago"


def fmt_clock(when: datetime, now: datetime, tz: tzinfo | None = None) -> str:
    """Local time: "14:05" today, "Mon 14:05" this week, else the full date."""
    local, today = when.astimezone(tz), now.astimezone(tz)
    days = (today.date() - local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if 0 < days < 7:
        return local.strftime("%a %H:%M")
    return local.strftime("%Y-%m-%d %H:%M")


# --- texts -----------------------------------------------------------------------


def reasons_text(reasons: Iterable[InstabilityReason]) -> str:
    text = ", ".join(r.value for r in reasons)
    return text[:1].upper() + text[1:]


def score_text(score: Score | None) -> str:
    if score is None:
        return DASH
    return f"{'~' if score.estimated else ''}{score.value} · {score.label}"


def state_detail(state: RouterState, location_allowed: bool) -> str:
    return {
        RouterState.ONLINE: "connected and tested",
        RouterState.VISIBLE: "in range, not tested",
        RouterState.NOT_FOUND: "not in the last Wi-Fi scan",
        RouterState.UNKNOWN: "location access is off" if not location_allowed else "can't scan",
    }[state]


def band_text(entry: ScanEntry) -> str:
    if entry.channel is None:
        return entry.band.value
    return f"{entry.band.value} · ch {entry.channel}"


def busy_text(busy: Busyness | None, entry: ScanEntry | None = None) -> str:
    if busy is None:
        return DASH
    load = entry.bss_load if entry else None
    if busy.estimated or load is None:
        return f"{busy.level.value} (estimated)"
    devices = "device" if load.station_count == 1 else "devices"
    return (
        f"{busy.level.value} ({load.station_count} {devices}, {load.utilization_pct:.0f}% airtime)"
    )


def quiet_hours_text(quiet: QuietHours) -> str:
    if not quiet.enabled:
        return "Off. Turn on to mute notifications at night."
    if quiet.start == quiet.end:
        return "The start and end are the same, so nothing is muted."
    return f"No notifications from {format_hhmm(quiet.start)} to {format_hhmm(quiet.end)}."


def fmt_hour(hour: int) -> str:
    return f"{hour:02d}:00"


def popular_times_summary(times: PopularTimes) -> str:
    recorded = times.hours_recorded
    if recorded == 0:
        return (
            "No data yet. Busyness is recorded while Router Checker runs "
            "and this router is in range."
        )
    window = times.busiest()
    if window is None:
        hours = "hour" if recorded == 1 else "hours"
        return f"Not enough data yet ({recorded} {hours} so far). This fills in over the week."
    if window.level is BusyLevel.LOW:
        return "Rarely busy: Low at every hour recorded so far."
    return (
        f"Usually busiest on {DAYS[window.day]}s, {fmt_hour(window.start_hour)}{DASH}"
        f"{fmt_hour(window.end_hour)} ({window.level.value})."
    )


def popular_cell_text(times: PopularTimes, day: int, hour: int) -> str:
    """One cell of the heatmap in words, e.g. "Tuesday 19:00–20:00: High (average of 3
    Tuesdays)"."""
    when = f"{DAYS[day]} {fmt_hour(hour)}{DASH}{fmt_hour(hour + 1)}"
    value = times.values[day][hour]
    if value is None:
        return f"{when}: no data yet"
    count = times.counts[day][hour]
    days = DAYS[day] if count == 1 else f"{DAYS[day]}s"
    return f"{when}: {busy_level(value).value} (average of {count} {days})"


def recommendation_text(rec: Recommendation, routers: Sequence[Router]) -> str:
    name = next((r.name for r in routers if r.id == rec.router_id), "Another router")
    if rec.margin is None:
        return f"{name} has the best score ({rec.score.value})"
    return f"{name} is {rec.margin} points better ({rec.score.value})"


# --- the current connection --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CurrentMetrics:
    gateway_ms: float | None
    gateway_p95_ms: float | None
    gateway_silent: bool
    gateway_loss_pct: float | None
    internet_ms: float | None
    internet_p95_ms: float | None
    loss_pct: float | None
    rssi: int | None
    signal_quality: int | None


def current_metrics(report: CycleReport | None) -> CurrentMetrics | None:
    if report is None or report.test is None or report.record is None:
        return None
    test, rec = report.test, report.record
    gw = test.gateway_ping
    p95s = [t.ping.p95_ms for t in test.targets if t.ping and t.ping.p95_ms is not None]
    return CurrentMetrics(
        gateway_ms=gw.median_ms if gw else None,
        gateway_p95_ms=gw.p95_ms if gw else None,
        gateway_silent=rec.gateway_silent,
        gateway_loss_pct=rec.gateway_loss_pct,
        internet_ms=rec.internet_latency_ms,
        internet_p95_ms=max(p95s) if p95s else None,
        loss_pct=rec.internet_loss_pct,
        rssi=test.rssi,
        signal_quality=test.signal_quality,
    )


def targets_text(report: CycleReport | None) -> str:
    """One line per internet target, for a tooltip."""
    if report is None or report.test is None:
        return ""
    lines = []
    for t in report.test.targets:
        if t.ping is None:
            lines.append(f"{t.target}: DNS lookup failed")
            continue
        if t.ping.all_lost:
            line = f"{t.target}: no answer"
        else:
            line = f"{t.target}: {fmt_ms(t.ping.median_ms)}, {fmt_pct(t.ping.loss_pct)} loss"
        if t.dns is not None and t.dns.ok:
            line += f", DNS {fmt_ms(t.dns.elapsed_ms)}"
        lines.append(line)
    return "\n".join(lines)


def tray_tooltip(report: CycleReport | None, status: Status, checking: bool = False) -> str:
    lines = ["Router Checker"]
    router = report.match.router if report else None
    lines.append(f"{router.name}: {status.title}" if router else status.title)
    metrics = current_metrics(report)
    if metrics is not None and router is not None:
        current = report.current if report else None
        score = f"Score {current.score.value} · " if current and current.score else ""
        lines.append(
            f"{score}Gateway {fmt_ms(metrics.gateway_ms)} · Internet {fmt_ms(metrics.internet_ms)}"
        )
    if checking:
        lines.append("Checking now…")
    text = "\n".join(lines)
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"


# --- nearby networks (Add router) ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class NearbyNetwork:
    ssid: str
    bssids: tuple[MacAddress, ...]  # strongest first
    best_rssi: int
    bands: tuple[Band, ...]
    has_profile: bool  # Windows has a saved profile with this name
    router_name: str | None  # already added as this router


def group_networks(
    entries: Iterable[ScanEntry], profiles: Iterable[str], routers: Sequence[Router]
) -> list[NearbyNetwork]:
    """Visible networks grouped by SSID (hidden networks left out), strongest first."""
    groups: dict[str, list[ScanEntry]] = {}
    for entry in entries:
        if entry.ssid:
            groups.setdefault(entry.ssid, []).append(entry)
    saved = set(profiles)
    band_order = list(Band)
    result = []
    for ssid, items in groups.items():
        items.sort(key=lambda e: e.rssi, reverse=True)
        owner = next(
            (r.name for r in routers if r.ssid == ssid or any(e.bssid in r.macs for e in items)),
            None,
        )
        result.append(
            NearbyNetwork(
                ssid=ssid,
                bssids=tuple(dict.fromkeys(e.bssid for e in items)),
                best_rssi=items[0].rssi,
                bands=tuple(sorted({e.band for e in items}, key=band_order.index)),
                has_profile=ssid in saved,
                router_name=owner,
            )
        )
    result.sort(key=lambda n: n.best_rssi, reverse=True)
    return result
