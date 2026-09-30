"""Switching Wi-Fi networks: "Test all now", the Switch button, and going back after
an interrupted Test all.

The PC has one Wi-Fi adapter, so testing another router means connecting to it for
a moment. Only profiles Windows already saved are used: the password stays in
Windows and is never read. Test all always restores the original connection, also
after an error, Cancel or Exit. While it is switched away, a small marker file
names the original network, so the next start can go back if the app couldn't.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path

from router_checker.core.checker import CheckEngine
from router_checker.core.errors import (
    CheckCancelled,
    LocationPermissionError,
    WifiUnavailableError,
)
from router_checker.core.matching import identify_current
from router_checker.core.models import (
    CheckRecord,
    Event,
    GatewayInfo,
    Router,
    SavedNetwork,
    ScanEntry,
    WifiConnection,
)
from router_checker.core.protocols import (
    Clock,
    HistoryStore,
    NetworkInfoService,
    RouterSwitcher,
    WifiService,
)

CONNECT_TIMEOUT_S = 20.0  # for the connection, an IP address and a gateway
SETTLE_S = 2.0  # then a short pause, so the first pings don't meet a half-ready link
POLL_S = 0.25
RESTORE_TRIES = 2
SECONDS_PER_ROUTER = 30  # rough: connecting, settling and the full test
SECONDS_TO_FINISH = 20  # reconnecting, then the check of the router you're on
MARKER_MAX_AGE = timedelta(hours=2)  # after that, you may have moved on: don't switch back
NO_CONNECTION = f"no connection within {CONNECT_TIMEOUT_S:.0f} seconds"
LOCATION_BLOCKER = (
    "Test all needs location access. Without it, Windows doesn't tell which Wi-Fi "
    "network you're on, so Router Checker couldn't switch back afterwards."
)
ON_ETHERNET = (
    "You're on Ethernet, so your internet goes over the cable: switching Wi-Fi "
    "wouldn't change it. Test all and switching are off until you're back on Wi-Fi."
)
_WINDOWS_ERRORS = (LocationPermissionError, WifiUnavailableError, OSError)

# Event kinds in the history.
TEST_ALL_EVENT = "test_all"  # one per run; the scheduler reads the last one
TESTED_EVENT = "tested"  # one per router Test all switched to
SWITCH_EVENT = "switch"


def no_profile_blocker(ssid: str) -> str:
    return (
        f"You're on “{ssid}” without a profile Windows saved, so Router Checker "
        "couldn't reconnect to it afterwards."
    )


# --- planning ----------------------------------------------------------------------


class SkipReason(StrEnum):
    NO_WIFI_NAME = "no Wi-Fi name"
    SAME_NAME = "same Wi-Fi name"
    NO_PROFILE = "not saved in Windows"
    NOT_IN_RANGE = "not in range"


SKIP_TEXTS = {
    SkipReason.NO_WIFI_NAME: "No Wi-Fi name yet. Add it with Edit.",
    SkipReason.SAME_NAME: "Same Wi-Fi name as another network.",
    SkipReason.NO_PROFILE: "Windows hasn't saved it. Connect to it once from the taskbar.",
    SkipReason.NOT_IN_RANGE: "Not in range right now.",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """A router Test all (or the Switch button) can connect to."""

    router: Router
    ssid: str
    profile_name: str


@dataclass(frozen=True, slots=True)
class Skipped:
    router: Router
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class TestAllPlan:
    __test__ = False  # not a pytest test class

    origin: WifiConnection | None  # where to go back to; None: Wi-Fi wasn't connected
    current: Router | None  # the router you're on: tested in place, without switching
    to_test: tuple[Candidate, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    blocker: str | None = None  # why nothing can be switched to at all

    @property
    def can_run(self) -> bool:
        return self.blocker is None and bool(self.to_test)

    @property
    def estimated_seconds(self) -> int:
        return SECONDS_PER_ROUTER * len(self.to_test) + SECONDS_TO_FINISH


def wifi_name(router: Router, scan: Sequence[ScanEntry]) -> str | None:
    """The router's Wi-Fi name, else the name one of its known BSSIDs broadcasts."""
    if router.ssid:
        return router.ssid
    named = [e for e in scan if e.ssid and e.bssid in router.macs]
    return max(named, key=lambda e: e.rssi).ssid if named else None


def profile_for(ssid: str, saved: Sequence[SavedNetwork]) -> str | None:
    """The saved profile for this Wi-Fi name (the one named like it first), else a
    profile named like it whose Wi-Fi name couldn't be read."""
    matches = [n.profile_name for n in saved if n.ssid == ssid]
    if matches:
        return ssid if ssid in matches else matches[0]
    return next((n.profile_name for n in saved if n.ssid is None and n.profile_name == ssid), None)


def plan_test_all(
    routers: Sequence[Router],
    connection: WifiConnection | None,
    gateway: GatewayInfo | None,
    scan: Sequence[ScanEntry],
    saved: Sequence[SavedNetwork],
) -> TestAllPlan:
    """Which routers to switch to, which to skip and why."""
    current = identify_current(routers, connection, gateway).router if connection else None
    if connection is not None and not connection.profile_name:
        return TestAllPlan(connection, current, blocker=no_profile_blocker(connection.ssid))
    taken = {connection.ssid} if connection is not None else set()
    to_test: list[Candidate] = []
    skipped: list[Skipped] = []
    for router in routers:
        if current is not None and router.id == current.id:
            continue
        ssid = wifi_name(router, scan)
        profile = profile_for(ssid, saved) if ssid else None
        if ssid is None:
            skipped.append(Skipped(router, SkipReason.NO_WIFI_NAME))
        elif ssid in taken:
            skipped.append(Skipped(router, SkipReason.SAME_NAME))
        elif profile is None:
            skipped.append(Skipped(router, SkipReason.NO_PROFILE))
        elif not any(e.ssid == ssid or e.bssid in router.macs for e in scan):
            skipped.append(Skipped(router, SkipReason.NOT_IN_RANGE))  # hidden ones by BSSID
        else:
            to_test.append(Candidate(router, ssid, profile))
            taken.add(ssid)
    return TestAllPlan(connection, current, tuple(to_test), tuple(skipped))


def gather_plan(
    routers: Sequence[Router],
    wifi: WifiService,
    netinfo: NetworkInfoService,
    switcher: RouterSwitcher,
    scan: Sequence[ScanEntry] | None = None,
    stop: threading.Event | None = None,
) -> TestAllPlan:
    """Read the connection, the saved profiles and a scan (``scan`` reuses a recent
    one), then plan."""
    try:
        connection = wifi.current_connection()
        entries = list(scan) if scan is not None else wifi.scan(stop=stop)
        saved = switcher.saved_networks()
    except LocationPermissionError:
        return TestAllPlan(None, None, blocker=LOCATION_BLOCKER)
    except WifiUnavailableError as exc:
        return TestAllPlan(None, None, blocker=f"Wi-Fi isn't available: {exc}.")
    return plan_test_all(routers, connection, netinfo.wifi_gateway(), entries, saved)


# --- running -----------------------------------------------------------------------


class Stage(StrEnum):
    CONNECTING = "connecting"
    TESTING = "testing"
    RESTORING = "restoring"


@dataclass(frozen=True, slots=True)
class Progress:
    stage: Stage
    name: str  # the router, or the network being restored
    step: int  # 1-based router number; total + 1 while restoring
    total: int  # routers to switch to


@dataclass(frozen=True, slots=True)
class Outcome:
    router: Router
    record: CheckRecord | None  # None: it couldn't be tested
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class TestAllResult:
    __test__ = False

    plan: TestAllPlan
    outcomes: tuple[Outcome, ...]
    restored: bool  # back on the original network (or off Wi-Fi again, as before)
    cancelled: bool  # stopped before every planned router was tested
    linked: tuple[Router, ...] = ()  # routers with newly learned MACs
    exited: bool = False  # the app exited: reconnecting was only requested

    @property
    def tested(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.record is not None]

    @property
    def failed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.record is None]


@dataclass(frozen=True, slots=True)
class SwitchResult:
    router: Router
    ok: bool
    problem: str | None = None  # why it didn't switch
    restored: bool | None = None  # after a failed attempt: back on the original network?
    origin: WifiConnection | None = None


@dataclass(frozen=True, slots=True)
class SwitchTiming:
    connect_timeout_s: float = CONNECT_TIMEOUT_S
    settle_s: float = SETTLE_S
    poll_s: float = POLL_S


def usable_address(ip: str | None) -> bool:
    """A real address, not the 169.254.x.x Windows picks when DHCP fails."""
    if not ip:
        return False
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return not (address.is_link_local or address.is_unspecified)


class SwitchRunner:
    """Test all and single switches, with waiting and restoring done carefully."""

    def __init__(
        self,
        engine: CheckEngine,
        switcher: RouterSwitcher,
        wifi: WifiService,
        netinfo: NetworkInfoService,
        store: HistoryStore,
        clock: Clock,
        marker: MarkerFile | None = None,
        timing: SwitchTiming = SwitchTiming(),  # noqa: B008 (frozen, so sharing it is fine)
    ) -> None:
        self._engine = engine
        self._switcher = switcher
        self._wifi = wifi
        self._netinfo = netinfo
        self._store = store
        self._clock = clock
        self._marker = marker
        self._timing = timing

    # --- Test all ---------------------------------------------------------------------

    def test_all(
        self,
        plan: TestAllPlan,
        *,
        cancel: threading.Event,
        exiting: threading.Event,
        progress: Callable[[Progress], None] = lambda _p: None,
        scheduled: bool = False,
    ) -> TestAllResult:
        """Connect to each planned router, test it, then restore the original connection.

        ``cancel`` (Cancel, or Exit) ends the run after the current step; nothing from
        a test it interrupts is saved. The connection is restored in every case. Once
        ``exiting`` is set, reconnecting is only requested, not awaited, and the marker
        stays for the next start.
        """
        if not plan.can_run:
            raise ValueError("this plan has nothing to test")
        total = len(plan.to_test)
        outcomes: list[Outcome] = []
        linked: dict[str, Router] = {}
        if self._marker is not None:
            self._marker.save(RestoreMarker.for_plan(plan, self._clock.now()))
        try:
            for step, candidate in enumerate(plan.to_test, start=1):
                if cancel.is_set():
                    break
                done = self._test_one(candidate, step, total, cancel, progress)
                if done is None:
                    break  # cancelled in the middle
                outcome, link = done
                outcomes.append(outcome)
                if link is not None:
                    linked[link.id] = link
        finally:
            restored = self._restore(plan.origin, total, exiting, progress)
            if self._marker is not None and (restored or not exiting.is_set()):
                self._marker.clear()
        result = TestAllResult(
            plan,
            tuple(outcomes),
            restored,
            cancelled=len(outcomes) < total,
            linked=(*linked.values(),),
            exited=exiting.is_set(),
        )
        self._log(result, scheduled)
        return result

    def _test_one(
        self,
        candidate: Candidate,
        step: int,
        total: int,
        cancel: threading.Event,
        progress: Callable[[Progress], None],
    ) -> tuple[Outcome, Router | None] | None:
        """Test one router; None when cancelled meanwhile."""
        router = candidate.router
        progress(Progress(Stage.CONNECTING, router.name, step, total))
        try:
            self._switcher.connect(candidate.profile_name)
            joined = self.wait_for(candidate.ssid, cancel)
        except _WINDOWS_ERRORS as exc:
            return Outcome(router, None, f"Windows couldn't connect: {exc}"), None
        if joined is None:
            return None if cancel.is_set() else (Outcome(router, None, NO_CONNECTION), None)
        progress(Progress(Stage.TESTING, router.name, step, total))
        try:
            record, link = self._engine.test_other(router, *joined, stop=cancel)
        except CheckCancelled:
            return None
        return Outcome(router, record), link

    def _log(self, result: TestAllResult, scheduled: bool) -> None:
        now = self._clock.now()
        for outcome in result.outcomes:
            if outcome.record is not None:
                score = outcome.record.score
                text = f"Tested during Test all: {outcome.record.verdict.value}"
                if score is not None:
                    text += f", score {round(score)}"
            else:
                text = f"Test all couldn't connect: {outcome.problem}"
            self._store.add_event(Event(now, outcome.router.id, TESTED_EVENT, text))
        self._store.add_event(Event(now, None, TEST_ALL_EVENT, run_event_text(result, scheduled)))

    # --- one switch ---------------------------------------------------------------------

    def switch(
        self,
        plan: TestAllPlan,
        router: Router,
        *,
        stop: threading.Event,
        reason: str | None = None,  # an automatic switch: why, for the event log
    ) -> SwitchResult:
        """Connect to ``router`` (the Switch button, or automatically). If that fails,
        go back."""
        candidate = next((c for c in plan.to_test if c.router.id == router.id), None)
        if candidate is None:
            return SwitchResult(router, False, switch_blocker(plan, router), origin=plan.origin)
        problem = NO_CONNECTION
        try:
            self._switcher.connect(candidate.profile_name)
            joined = self.wait_for(candidate.ssid, stop)
        except _WINDOWS_ERRORS as exc:
            joined, problem = None, f"Windows couldn't connect: {exc}"
        now = self._clock.now()
        if joined is not None:
            text = f"Switched to {router.name}"
            if reason:
                text = f"Switched automatically to {router.name}: {reason}"
            self._store.add_event(Event(now, router.id, SWITCH_EVENT, text))
            return SwitchResult(router, True, origin=plan.origin)
        restored = self._restore(plan.origin, 0, stop, lambda _p: None)
        self._store.add_event(
            Event(now, router.id, SWITCH_EVENT, f"Couldn't switch to {router.name}: {problem}")
        )
        return SwitchResult(router, False, problem, restored, plan.origin)

    # --- waiting and restoring -----------------------------------------------------------

    def joined(self, ssid: str) -> tuple[WifiConnection, GatewayInfo] | None:
        """The connection and gateway if connected to ``ssid`` with a usable address."""
        connection = self._wifi.current_connection()
        if connection is None or connection.ssid != ssid:
            return None
        gateway = self._netinfo.wifi_gateway()
        if gateway is None or not usable_address(gateway.local_ip):
            return None
        return connection, gateway

    def wait_for(
        self, ssid: str, stop: threading.Event
    ) -> tuple[WifiConnection, GatewayInfo] | None:
        """Wait until connected to ``ssid`` with an address and a gateway, then settle.
        None after the timeout or once ``stop`` is set."""
        timing = self._timing
        deadline = time.monotonic() + timing.connect_timeout_s
        while True:
            if self.joined(ssid) is not None:
                if stop.wait(timing.settle_s):
                    return None
                joined = self.joined(ssid)  # read again: the address may just have changed
                if joined is not None:
                    return joined
            if time.monotonic() >= deadline or stop.wait(timing.poll_s):
                return None

    def _restore(
        self,
        origin: WifiConnection | None,
        total: int,
        exiting: threading.Event,
        progress: Callable[[Progress], None],
    ) -> bool:
        """Back to ``origin`` (or off Wi-Fi if it's None). Never raises."""
        try:
            if origin is None:
                if self._wifi.current_connection() is not None:
                    self._switcher.disconnect()
                return True
            progress(Progress(Stage.RESTORING, origin.ssid, total + 1, total))
            for _ in range(RESTORE_TRIES):
                if self.joined(origin.ssid) is not None:
                    return True
                self._switcher.connect(origin.profile_name)
                if exiting.is_set():
                    return False  # asked Windows to reconnect; no time to wait for it
                if self.wait_for(origin.ssid, exiting) is not None:
                    return True
            return False
        except _WINDOWS_ERRORS:
            return False

    # --- after an interrupted run ---------------------------------------------------------

    def recover(self, *, stop: threading.Event) -> Event | None:
        """At start: if Test all was interrupted while switched away (the app crashed or
        exited), go back to the original network. Returns the event it logged."""
        if self._marker is None:
            return None
        marker = self._marker.load()
        if marker is None:
            self._marker.clear()  # an unreadable one
            return None
        now = self._clock.now()
        try:
            action = recovery_for(marker, self._wifi.current_connection(), now)
            ok = True
            if action is Recovery.RECONNECT and marker.ssid and marker.profile_name:
                self._switcher.connect(marker.profile_name)
                ok = self.wait_for(marker.ssid, stop) is not None
            elif action is Recovery.DISCONNECT:
                self._switcher.disconnect()
        except _WINDOWS_ERRORS:
            action, ok = Recovery.NONE, False
        self._marker.clear()
        if action is Recovery.NONE:
            return None
        if action is Recovery.DISCONNECT:
            text = "Disconnected Wi-Fi again after an interrupted Test all"
        elif ok:
            text = f"Went back to “{marker.ssid}” after an interrupted Test all"
        else:
            text = f"Tried to go back to “{marker.ssid}” after an interrupted Test all"
        event = Event(now, None, SWITCH_EVENT, text)
        self._store.add_event(event)
        return event


def switch_blocker(plan: TestAllPlan, router: Router) -> str:
    """Why the Switch button can't connect to ``router``."""
    if plan.blocker is not None:
        return plan.blocker
    if plan.current is not None and plan.current.id == router.id:
        return f"You're already on {router.name}."
    skipped = next((s for s in plan.skipped if s.router.id == router.id), None)
    return SKIP_TEXTS[skipped.reason] if skipped else f"{router.name} isn't one of your routers."


def run_event_text(result: TestAllResult, scheduled: bool) -> str:
    """The run's line in the event log."""
    parts = [f"{'Scheduled test all' if scheduled else 'Test all'}:"]
    tested = [o.router.name for o in result.tested]
    parts.append(f"tested {', '.join(tested)}" if tested else "nothing tested")
    if result.failed:
        parts[-1] += f"; couldn't connect to {', '.join(o.router.name for o in result.failed)}"
    if result.cancelled:
        parts[-1] += "; cancelled"
    origin = result.plan.origin
    if origin is None:
        parts[-1] += "; Wi-Fi disconnected again" if result.restored else "; couldn't disconnect"
    elif result.restored:
        parts[-1] += f"; back on “{origin.ssid}”"
    elif result.exited:
        parts[-1] += f"; Router Checker exited, so it asked Windows to reconnect to “{origin.ssid}”"
    else:
        parts[-1] += f"; couldn't reconnect to “{origin.ssid}”"
    return " ".join(parts)


# --- the restore marker ------------------------------------------------------------


class Recovery(StrEnum):
    NONE = "none"
    RECONNECT = "reconnect"
    DISCONNECT = "disconnect"


@dataclass(frozen=True, slots=True)
class RestoreMarker:
    """Written before Test all switches away, deleted once it's back."""

    started: datetime
    ssid: str | None  # the original network; None when Wi-Fi wasn't connected
    profile_name: str | None
    tested: tuple[str, ...]  # the Wi-Fi names Test all switches to

    @classmethod
    def for_plan(cls, plan: TestAllPlan, now: datetime) -> RestoreMarker:
        origin = plan.origin
        return cls(
            now,
            origin.ssid if origin else None,
            origin.profile_name if origin else None,
            tuple(c.ssid for c in plan.to_test),
        )


def recovery_for(
    marker: RestoreMarker, connection: WifiConnection | None, now: datetime
) -> Recovery:
    """Go back only from a network Test all switched to, and only soon after."""
    if now - marker.started > MARKER_MAX_AGE:
        return Recovery.NONE
    if connection is None or connection.ssid not in marker.tested:
        return Recovery.NONE
    if marker.ssid is None:
        return Recovery.DISCONNECT
    return Recovery.NONE if connection.ssid == marker.ssid else Recovery.RECONNECT


class MarkerFile:
    """The marker as a small JSON file in the data folder."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, marker: RestoreMarker) -> None:
        data = {
            "started": marker.started.isoformat(),
            "ssid": marker.ssid,
            "profile_name": marker.profile_name,
            "tested": list(marker.tested),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self) -> RestoreMarker | None:
        """The marker, or None if there is none (or it can't be read)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(data["started"])
            if started.tzinfo is None:
                raise ValueError("the marker time has no time zone")
            return RestoreMarker(
                started,
                data.get("ssid"),
                data.get("profile_name"),
                tuple(str(s) for s in data.get("tested", ())),
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def clear(self) -> None:
        # If it can't be deleted: it only matters within MARKER_MAX_AGE, on a tested network.
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)
