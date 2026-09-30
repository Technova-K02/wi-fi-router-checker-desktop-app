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
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from enum import StrEnum

from router_checker.core.auto_switch import (
    AVOID_AFTER_FAILURE,
    BETTER_CHECKS,
    DOWN_CHECKS,
    Decision,
    Reason,
)
from router_checker.core.auto_switch import Progress as AutoProgress
from router_checker.core.checker import CycleReport
from router_checker.core.mac import MacAddress
from router_checker.core.middle import colon_mac
from router_checker.core.models import (
    Band,
    BusyLevel,
    Busyness,
    InstabilityReason,
    LinkChoice,
    LinkKind,
    Recommendation,
    Router,
    RouterState,
    ScanEntry,
    Score,
    Verdict,
    WifiConnection,
)
from router_checker.core.popularity import PopularTimes, busy_level
from router_checker.core.quiet_hours import QuietHours, format_hhmm
from router_checker.core.scoring import LABEL_GOOD
from router_checker.core.series import Series
from router_checker.core.switching import (
    SKIP_TEXTS,
    MiddleOrigin,
    Progress,
    Stage,
    SwitchResult,
    TestAllPlan,
    TestAllResult,
)

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


NOT_CONNECTED = {
    LinkChoice.AUTO: "No Wi-Fi or Ethernet connection to a router.",
    LinkChoice.WIFI: "Wi-Fi isn't connected. Settings > Connection is set to Wi-Fi only.",
    LinkChoice.ETHERNET: "No cable is connected. Settings > Connection is set to Ethernet only.",
}


def overall_status(report: CycleReport | None, failure: str | None = None) -> Status:
    """Status of the connection right now (tray icon, flyout, dashboard)."""
    if failure is not None:
        return Status(StatusLevel.UNKNOWN, "Check failed", failure)
    if report is None:
        return Status(StatusLevel.UNKNOWN, "Waiting for the first check")
    record = report.record
    if report.gateway is None or record is None:
        return Status(
            StatusLevel.UNKNOWN, "Not connected", NOT_CONNECTED[report.settings.connection]
        )
    if report.match.router is None and report.middle is not None:
        if report.upstream_ip is None:
            detail = "Couldn't tell which router the middle router is on (no second hop)."
        else:
            detail = f"The middle router is on a router at {report.upstream_ip}, not one of yours."
        return Status(StatusLevel.UNKNOWN, "Unknown router", detail)
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


def series_summary(
    series: Series, fmt: Callable[[float | None], str], *, lower_is_worse: bool = False
) -> str:
    """The numbers behind a chart line in words, e.g. "Internet: median 24 ms, highest
    120 ms"."""
    values = series.values
    if not values:
        return f"{series.name}: no data"
    worst, word = (min(values), "lowest") if lower_is_worse else (max(values), "highest")
    return f"{series.name}: median {fmt(statistics.median(values))}, {word} {fmt(worst)}"


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
    wired: bool = False
    via_vpn: bool = False  # internet numbers include the VPN
    middle_ms: float | None = None  # to the middle router, when the PC is behind it
    behind_middle: bool = False


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
        wired=rec.link is LinkKind.ETHERNET,
        via_vpn=rec.via_vpn,
        middle_ms=test.middle_ping.median_ms if test.middle_ping else None,
        behind_middle=report.middle is not None,
    )


def vpn_text(report: CycleReport | None) -> str | None:
    """ "VPN on", when a VPN carries the internet traffic; the router is checked past it."""
    if report is None or report.gateway is None or not report.gateway.vpn:
        return None
    if report.record is not None and report.record.via_vpn:
        return "VPN on, internet measured through it"
    return "VPN on"


def no_recommendation_text(report: CycleReport | None) -> str:
    wired = report is not None and report.gateway is not None and report.gateway.wired
    return "Not while on Ethernet" if wired else "None right now"


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


def tray_tooltip(
    report: CycleReport | None,
    status: Status,
    checking: bool = False,
    activity: str | None = None,  # e.g. Test all's progress; replaces "Checking now…"
) -> str:
    lines = ["Router Checker"]
    router = report.match.router if report else None
    wired = report is not None and report.gateway is not None and report.gateway.wired
    name = f"{router.name} (Ethernet)" if router and wired else router.name if router else ""
    lines.append(f"{name}: {status.title}" if router else status.title)
    metrics = current_metrics(report)
    if metrics is not None and router is not None:
        current = report.current if report else None
        score = f"Score {current.score.value} · " if current and current.score else ""
        lines.append(
            f"{score}Gateway {fmt_ms(metrics.gateway_ms)} · Internet {fmt_ms(metrics.internet_ms)}"
        )
    if activity:
        lines.append(activity)
    elif checking:
        lines.append("Checking now…")
    text = "\n".join(lines)
    return text if len(text) <= TOOLTIP_MAX else text[: TOOLTIP_MAX - 1] + "…"


# --- Test all and switching -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Message:
    """A dialog, info bar or notification: level, title and text."""

    level: StatusLevel
    title: str
    text: str
    restore_failed: bool = False  # the original connection couldn't be restored
    back_to: str | None = None  # offer to go back to this router (automatic switch)


def join_names(names: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def fmt_duration(seconds: float) -> str:
    minutes = math.ceil(seconds / 60)
    return "about a minute" if minutes <= 1 else f"about {minutes} minutes"


def _quoted(ssid: str) -> str:
    return f"“{ssid}”"


def _back_on(origin: WifiConnection | MiddleOrigin) -> str:
    if isinstance(origin, MiddleOrigin):
        return f"The middle router is back on {origin.router.name}."
    return f"Back on {_quoted(origin.ssid)}."


def confirm_test_all(plan: TestAllPlan) -> Message:
    """The question before Test all starts, or why it can't."""
    skipped = [f"• {s.router.name}: {SKIP_TEXTS[s.reason]}" for s in plan.skipped]
    if plan.blocker is not None:
        return Message(StatusLevel.WARNING, "Test all can't run", plan.blocker)
    if not plan.to_test:
        rule = (
            "Test all switches your middle router only to routers that have a Wi-Fi MAC "
            "and are in range."
            if plan.via_middle
            else "Test all switches only to routers that have a Wi-Fi name, are in range, "
            "and are saved in Windows."
        )
        return Message(
            StatusLevel.UNKNOWN, "No other router to test", "\n".join([rule, "", *skipped])
        )
    origin = plan.origin
    names = [c.router.name for c in plan.to_test]
    if isinstance(origin, MiddleOrigin):
        names.append(f"{origin.router.name} (the middle router is on it, so no switch)")
        intro = (
            "Router Checker asks your middle router to switch to each router in turn, tests "
            f"it, then switches it back to {origin.router.name}. The internet drops for a few "
            "seconds at each switch, for every device behind the middle router."
        )
    else:
        back = f"reconnects to {_quoted(origin.ssid)}" if origin else "disconnects Wi-Fi again"
        if plan.current is not None:
            names.append(f"{plan.current.name} (you're on it, so no switch)")
        intro = (
            f"Router Checker connects to each router in turn, tests it, then {back}. "
            "Your internet drops for a few seconds at each switch."
        )
    lines = [
        f"{intro} This takes {fmt_duration(plan.estimated_seconds)}.",
        "",
        f"To test: {join_names(names)}.",
    ]
    if skipped:
        lines += ["Skipped:", *skipped]
    return Message(StatusLevel.UNKNOWN, "Test all routers?", "\n".join(lines))


def progress_text(progress: Progress | None) -> str:
    if progress is None:
        return "Starting Test all…"
    position = f"({progress.step} of {progress.total})"
    if progress.stage is Stage.CONNECTING:
        return f"Connecting to {progress.name} {position}…"
    if progress.stage is Stage.TESTING:
        return f"Testing {progress.name} {position}…"
    if progress.middle:
        return f"Switching the middle router back to {progress.name}…"
    return f"Reconnecting to {_quoted(progress.name)}…"


def progress_steps(progress: Progress | None, total: int) -> tuple[int, int]:
    """(done, all) steps for a progress bar: connect and test for every router, then
    reconnecting and the check of the router you're on."""
    steps = 2 * total + 2
    if progress is None:
        return 0, steps
    if progress.stage is Stage.RESTORING:
        return 2 * total, steps
    return 2 * (progress.step - 1) + (progress.stage is Stage.TESTING), steps


def summarize_test_all(
    result: TestAllResult | None, report: CycleReport | None, error: str | None = None
) -> Message:
    """How Test all went, after the final check of the router you're on.

    ``result`` is None when the run failed (``error``) before it could report.
    """
    now_on = report.connection.ssid if report and report.connection else None
    if result is None:
        where = f" You're on {_quoted(now_on)}." if now_on else ""
        return Message(StatusLevel.BAD, "Test all stopped", f"{error or 'It failed'}.{where}")
    origin = result.plan.origin
    if not result.restored and not result.exited:
        if isinstance(origin, MiddleOrigin):
            return Message(
                StatusLevel.BAD,
                f"Couldn't switch back to {origin.router.name}",
                f"Test all couldn't switch your middle router back. {_switch_back_hint(origin)}",
                restore_failed=True,
            )
        if origin is None:
            return Message(
                StatusLevel.WARNING,
                "Wi-Fi is still connected",
                "Test all couldn't disconnect Wi-Fi again, as it was before.",
                restore_failed=True,
            )
        return Message(
            StatusLevel.BAD,
            f"Couldn't reconnect to {_quoted(origin.ssid)}",
            f"Test all couldn't switch back. Connect to {_quoted(origin.ssid)} "
            "from the Wi-Fi menu on the taskbar.",
            restore_failed=True,
        )

    tested = [o.router for o in result.tested]
    current = report.match.router if report and report.record else None
    if current is not None and all(r.id != current.id for r in tested):
        tested.append(current)
    parts = [f"Tested {join_names([r.name for r in tested])}." if tested else "Nothing was tested."]
    for outcome in result.failed:
        parts.append(f"Couldn't connect to {outcome.router.name} ({outcome.problem}).")
    best = _best_tested(report, {r.id for r in tested})
    if best is not None:
        router, score = best
        mine = " (the one you're on)" if current is not None and router.id == current.id else ""
        parts.append(f"Best: {router.name}{mine}, score {score.value}.")
    parts.append("Wi-Fi is disconnected again." if origin is None else _back_on(origin))
    title = "Test all cancelled" if result.cancelled else "Test all finished"
    level = StatusLevel.WARNING if result.cancelled or result.failed else StatusLevel.GOOD
    return Message(level, title, " ".join(parts))


def _switch_back_hint(origin: MiddleOrigin) -> str:
    mac = colon_mac(origin.bssids[0]) if origin.bssids else "its Wi-Fi MAC"
    return f"Switch it to {origin.router.name} from its own page (change_router?router={mac})."


def _best_tested(report: CycleReport | None, ids: set[str]) -> tuple[Router, Score] | None:
    if report is None:
        return None
    measured = [
        (s.router, s.score)
        for s in report.statuses
        if s.router.id in ids and s.score is not None and not s.score.estimated
    ]
    return max(measured, key=lambda item: item[1].value) if measured else None


def switch_summary(result: SwitchResult) -> Message:
    name = result.router.name
    if result.ok:
        return Message(StatusLevel.GOOD, f"Switched to {name}", "Checking it now.")
    problem = result.problem or "It failed"
    if result.restored is None:  # nothing was changed
        return Message(StatusLevel.WARNING, f"Can't switch to {name}", problem)
    problem = f"{problem[:1].upper()}{problem[1:]}."
    origin = result.origin
    if result.restored:
        back = f" {_back_on(origin)}" if origin else ""
        return Message(StatusLevel.WARNING, f"Couldn't switch to {name}", problem + back)
    if isinstance(origin, MiddleOrigin):
        return Message(
            StatusLevel.BAD,
            f"Couldn't switch to {name}",
            f"{problem} Couldn't switch the middle router back to {origin.router.name} "
            f"either. {_switch_back_hint(origin)}",
            restore_failed=True,
        )
    where = _quoted(origin.ssid) if origin else "a network"
    return Message(
        StatusLevel.BAD,
        f"Couldn't switch to {name}",
        f"{problem} Couldn't reconnect to {where} either. "
        "Connect from the Wi-Fi menu on the taskbar.",
        restore_failed=True,
    )


# --- automatic switching -------------------------------------------------------------

_VERDICT_WORDS = {
    Verdict.UNSTABLE: "unstable",
    Verdict.ROUTER_UNREACHABLE: "not responding",
    Verdict.INTERNET_DOWN: "no internet",
}


def auto_switch_reason(decision: Decision, names: Mapping[str, str]) -> str:
    """Why an automatic switch happens, for the event log and the notification."""
    to = names.get(decision.router_id or "", "the other router")
    current = names.get(decision.from_id or "", "your router")
    words = _VERDICT_WORDS.get(decision.verdict, "failing") if decision.verdict else ""
    if decision.reason is Reason.DOWN:
        return f"{current} was {words} for {DOWN_CHECKS} checks in a row"
    if decision.reason is Reason.FAILED_AFTER_SWITCH:
        return f"{current} was {words} right after the switch"
    score = decision.score.value if decision.score else None
    theirs = decision.current_score.value if decision.current_score else None
    numbers = f" (score {score} vs {theirs})" if score is not None and theirs is not None else ""
    return f"{to} was clearly better for {BETTER_CHECKS} checks in a row{numbers}"


def auto_switch_message(decision: Decision, names: Mapping[str, str]) -> Message:
    """The notification after an automatic switch that worked."""
    to = names.get(decision.router_id or "", "the other router")
    reason = auto_switch_reason(decision, names)
    if decision.reason is Reason.FAILED_AFTER_SWITCH:
        hours = round(AVOID_AFTER_FAILURE.total_seconds() / 3600)
        failed = names.get(decision.from_id or "", "It")
        return Message(
            StatusLevel.WARNING,
            f"Back on {to}",
            f"{reason[:1].upper()}{reason[1:]}. {failed} won't be picked automatically "
            f"for {hours} hours.",
        )
    return Message(
        StatusLevel.GOOD,
        f"Switched to {to}",
        f"{reason[:1].upper()}{reason[1:]}.",
        back_to=decision.from_id,
    )


def auto_switch_progress(progress: AutoProgress | None, names: Mapping[str, str]) -> str | None:
    """A line for the dashboard while automatic switching waits or builds up."""
    if progress is None:
        return None
    if progress.cooldown_left is not None:
        minutes = max(1, math.ceil(progress.cooldown_left.total_seconds() / 60))
        return f"Automatic switching pauses for {minutes} more min after the last switch."
    if progress.router_id is None or not progress.checks:
        return None
    name = names.get(progress.router_id, "Another router")
    return f"{name} has been better for {progress.checks} of {BETTER_CHECKS} checks."


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
