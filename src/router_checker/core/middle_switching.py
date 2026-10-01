"""Test all and switching behind the middle router (see ``core.middle``).

The middle router is asked to join a router by its Wi-Fi MAC. The switch counts
once the second hop shows that router: its known address, or (the first time)
an address that is new and belongs to no other router, which is then learned.
A router's Wi-Fi MACs are tried best first (``bssid_order``). Going back works
the same way, twice through the origin's MACs. While switched away, a marker
file names the origin, so the next start can go back if the app couldn't.

Switching the middle router moves every device behind it, not only this PC.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from router_checker.core.checker import CheckEngine
from router_checker.core.errors import (
    CheckCancelled,
    LocationPermissionError,
    WifiUnavailableError,
)
from router_checker.core.mac import MacAddress
from router_checker.core.middle import Endpoint, bssid_order, router_at
from router_checker.core.models import Event, GatewayInfo, Router, ScanEntry
from router_checker.core.protocols import (
    Clock,
    HistoryStore,
    MiddleRouterClient,
    NetworkInfoService,
    WifiService,
)
from router_checker.core.switching import (
    MARKER_MAX_AGE,
    NO_CONNECTION,
    RESTORE_TRIES,
    SWITCH_EVENT,
    Candidate,
    MiddleOrigin,
    Outcome,
    Progress,
    RunnerBase,
    Skipped,
    SkipReason,
    Stage,
    SwitchResult,
    SwitchTiming,
    TestAllPlan,
)

NOT_BEHIND = "You're not behind your middle router right now."
NOT_ACCEPTED = "the middle router didn't accept the switch"


def unknown_origin(address: str | None) -> str:
    if address is None:
        return (
            "Router Checker can't tell which of your routers the middle router is on (the "
            "second hop didn't answer), so it couldn't switch back afterwards."
        )
    return (
        f"The middle router is on a router at {address} that isn't one of yours (or two "
        "of yours use that address), so Router Checker couldn't switch back afterwards. "
        "Add the address to that router with Edit."
    )


def no_way_back(router: Router) -> str:
    return (
        f"Router Checker has no Wi-Fi MAC for {router.name}, so it couldn't ask the middle "
        "router to switch back to it. Add its Wi-Fi MAC with Edit."
    )


# --- planning ----------------------------------------------------------------------


def plan_middle(
    routers: Sequence[Router],
    address: str | None,
    scan: Sequence[ScanEntry] | None,
    observed: Mapping[str, MacAddress] | None = None,
) -> TestAllPlan:
    """Which routers the middle router can be switched to: every one with a Wi-Fi MAC.
    ``address`` is the router it's on now (the second hop). ``scan`` (None when this PC
    can't scan) and ``observed`` (the Wi-Fi MAC last seen per router) only order the
    MACs: what this PC sees doesn't tell what the middle router can reach."""
    observed = observed or {}
    current = router_at(routers, address)
    if current is None or address is None:
        return TestAllPlan(None, None, blocker=unknown_origin(address))
    origin = MiddleOrigin(current, address, bssid_order(current, scan, observed.get(current.id)))
    if not origin.bssids:
        return TestAllPlan(origin, current, blocker=no_way_back(current))
    to_test: list[Candidate] = []
    skipped: list[Skipped] = []
    for router in routers:
        if router.id == current.id:
            continue
        bssids = bssid_order(router, scan, observed.get(router.id))
        if not bssids:
            skipped.append(Skipped(router, SkipReason.NO_WIFI_MAC))
        else:
            to_test.append(Candidate(router, "", "", bssids))
    return TestAllPlan(origin, current, tuple(to_test), tuple(skipped))


def gather_middle_plan(
    engine: CheckEngine,
    netinfo: NetworkInfoService,
    wifi: WifiService | None,
    store: HistoryStore,
    scan: Sequence[ScanEntry] | None = None,
    stop: threading.Event | None = None,
) -> TestAllPlan:
    """Find the router the middle router is on, scan if this PC can, then plan."""
    s = engine.settings
    gateway = netinfo.gateway(s.connection)
    if s.middle is None or not s.middle.is_gateway(gateway):
        return TestAllPlan(None, None, blocker=NOT_BEHIND)
    assert gateway is not None
    address = engine.upstream_address(gateway, stop)
    if scan is None and wifi is not None:
        try:
            scan = wifi.scan(stop=stop)
        except (LocationPermissionError, WifiUnavailableError, OSError):
            scan = None  # no Wi-Fi, or no location access: try every router with a MAC
    observed: dict[str, MacAddress] = {}
    for router in s.routers:
        observation = store.latest_observation(router.id)
        if observation is not None:
            observed[router.id] = observation.entry.bssid
    return plan_middle(s.routers, address, scan, observed)


# --- running -----------------------------------------------------------------------


class MiddleRunner(RunnerBase):
    """Switching by asking the middle router, with waiting and restoring done carefully."""

    def __init__(
        self,
        engine: CheckEngine,
        client: MiddleRouterClient,
        netinfo: NetworkInfoService,
        store: HistoryStore,
        clock: Clock,
        marker: MiddleMarkerFile | None = None,
        timing: SwitchTiming = SwitchTiming(),  # noqa: B008 (frozen, so sharing it is fine)
    ) -> None:
        super().__init__(engine, store, clock, timing)
        self._client = client
        self._netinfo = netinfo
        self._marker = marker
        self._here: str | None = None  # the address the second hop showed last

    @property
    def _endpoint(self) -> Endpoint:
        endpoint = self._engine.settings.middle
        if endpoint is None:
            raise OSError("no middle router is set up")
        return endpoint

    # --- where we are -------------------------------------------------------------

    def arrived(
        self, router: Router, previous: str | None, stop: threading.Event
    ) -> tuple[GatewayInfo, str] | None:
        """(gateway, address) once the second hop shows ``router``: its address, or,
        if it has none yet, a new one no other router has."""
        s = self._engine.settings
        gateway = self._netinfo.gateway(s.connection)
        if gateway is None or s.middle is None or not s.middle.is_gateway(gateway):
            return None
        hop = self._engine.upstream_address(gateway, stop)
        if hop is None:
            return None
        if router.address is not None:
            return (gateway, hop) if hop == router.address else None
        others = {r.address for r in s.routers if r.id != router.id and r.address}
        return (gateway, hop) if hop != previous and hop not in others else None

    def wait_for(
        self, router: Router, previous: str | None, stop: threading.Event
    ) -> tuple[GatewayInfo, str] | None:
        """Wait until the middle router is on ``router``, then settle. None after the
        timeout or once ``stop`` is set."""
        timing = self._timing
        deadline = time.monotonic() + timing.connect_timeout_s
        while True:
            if self.arrived(router, previous, stop) is not None:
                if stop.wait(timing.settle_s):
                    return None
                arrived = self.arrived(router, previous, stop)  # still there after the pause?
                if arrived is not None:
                    return arrived
            if time.monotonic() >= deadline or stop.wait(timing.poll_s):
                return None

    def _switch_to(
        self,
        router: Router,
        bssids: Sequence[MacAddress],
        previous: str | None,
        stop: threading.Event,
    ) -> tuple[tuple[GatewayInfo, str] | None, MacAddress | None, str]:
        """Ask for each Wi-Fi MAC in turn until the middle router is on ``router``:
        (gateway and address, the MAC that worked, why not)."""
        problem = NO_CONNECTION
        for bssid in bssids:
            if stop.is_set():
                break
            try:
                self._client.change_router(self._endpoint, bssid)
            except OSError as exc:
                problem = f"{NOT_ACCEPTED} ({exc})"
                continue
            try:
                arrived = self.wait_for(router, previous, stop)
            except CheckCancelled:
                break
            if arrived is not None:
                return arrived, bssid, ""
            problem = NO_CONNECTION
        return None, None, problem

    # --- RunnerBase -------------------------------------------------------------------

    def switch(self, plan: TestAllPlan, router: Router, **kwargs) -> SwitchResult:
        self._here = plan.origin.address if isinstance(plan.origin, MiddleOrigin) else None
        return super().switch(plan, router, **kwargs)

    def _mark(self, plan: TestAllPlan) -> None:
        origin = plan.origin
        assert isinstance(origin, MiddleOrigin)
        self._here = origin.address
        if self._marker is not None:
            self._marker.save(
                MiddleMarker(
                    self._clock.now(),
                    origin.router.id,
                    origin.address,
                    origin.bssids,
                    tuple(c.router.id for c in plan.to_test),
                )
            )

    def _unmark(self) -> None:
        if self._marker is not None:
            self._marker.clear()

    def _test_one(
        self,
        candidate: Candidate,
        step: int,
        total: int,
        cancel: threading.Event,
        progress: Callable[[Progress], None],
    ) -> tuple[Outcome, Router | None] | None:
        router = candidate.router
        progress(Progress(Stage.CONNECTING, router.name, step, total, middle=True))
        arrived, bssid, problem = self._switch_to(router, candidate.bssids, self._here, cancel)
        if arrived is None:
            return None if cancel.is_set() else (Outcome(router, None, problem), None)
        gateway, address = arrived
        self._here = address
        progress(Progress(Stage.TESTING, router.name, step, total, middle=True))
        try:
            record, learned = self._engine.test_behind(router, gateway, address, bssid, cancel)
        except CheckCancelled:
            return None
        return Outcome(router, record), learned

    def _go(self, candidate: Candidate, stop: threading.Event) -> tuple[bool, str, Router | None]:
        router = candidate.router
        arrived, bssid, problem = self._switch_to(router, candidate.bssids, self._here, stop)
        if arrived is None:
            return False, problem, None
        _gateway, address = arrived
        learned = self._engine.learn_behind(router, address, bssid, self._clock.now())
        return True, "", learned

    def _restore(
        self,
        plan: TestAllPlan,
        total: int,
        exiting: threading.Event,
        progress: Callable[[Progress], None],
    ) -> bool:
        origin = plan.origin
        assert isinstance(origin, MiddleOrigin)
        return self._go_back(origin, total, exiting, progress)

    def _go_back(
        self,
        origin: MiddleOrigin,
        total: int,
        exiting: threading.Event,
        progress: Callable[[Progress], None],
    ) -> bool:
        """Back to ``origin``. Once ``exiting`` is set, only asks. Never raises."""
        target = replace(origin.router, address=origin.address)
        try:
            # When exiting there's no time to look first: just ask.
            if not exiting.is_set() and self.arrived(target, None, exiting) is not None:
                return True
            progress(Progress(Stage.RESTORING, origin.router.name, total + 1, total, middle=True))
            for _ in range(RESTORE_TRIES):
                for bssid in origin.bssids:
                    try:
                        self._client.change_router(self._endpoint, bssid)
                    except OSError:
                        continue
                    if exiting.is_set():
                        return False  # asked the middle router; no time to wait for it
                    if self.wait_for(target, None, exiting) is not None:
                        return True
            return False
        except (OSError, CheckCancelled):
            return False

    # --- after an interrupted run ---------------------------------------------------------

    def recover(self, *, stop: threading.Event) -> Event | None:
        """At start: if Test all was interrupted while the middle router was switched
        away, switch it back. Returns the event it logged."""
        if self._marker is None:
            return None
        marker = self._marker.load()
        clear = self._marker.clear
        if marker is None or self._clock.now() - marker.started > MARKER_MAX_AGE:
            clear()
            return None
        s = self._engine.settings
        origin_router = s.router(marker.origin_id)
        gateway = self._netinfo.gateway(s.connection)
        if origin_router is None or s.middle is None or not s.middle.is_gateway(gateway):
            clear()
            return None
        assert gateway is not None
        try:
            here = router_at(s.routers, self._engine.upstream_address(gateway, stop))
        except CheckCancelled:
            return None
        if here is None or here.id == origin_router.id or here.id not in marker.tested:
            clear()
            return None
        origin = MiddleOrigin(origin_router, marker.origin_address, marker.bssids)
        ok = self._go_back(origin, 0, stop, lambda _p: None)
        clear()
        verb = "Switched" if ok else "Tried to switch"
        text = (
            f"{verb} the middle router back to {origin_router.name} after an interrupted Test all"
        )
        event = Event(self._clock.now(), None, SWITCH_EVENT, text)
        self._store.add_event(event)
        return event


# --- the restore marker ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MiddleMarker:
    """Written before Test all switches the middle router away, deleted once it's back."""

    started: datetime
    origin_id: str
    origin_address: str
    bssids: tuple[MacAddress, ...]  # to ask for when going back
    tested: tuple[str, ...]  # ids of the routers Test all switches to


class MiddleMarkerFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, marker: MiddleMarker) -> None:
        data = {
            "started": marker.started.isoformat(),
            "origin_id": marker.origin_id,
            "origin_address": marker.origin_address,
            "bssids": [str(m) for m in marker.bssids],
            "tested": list(marker.tested),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def load(self) -> MiddleMarker | None:
        """The marker, or None if there is none (or it can't be read)."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(data["started"])
            if started.tzinfo is None:
                raise ValueError("the marker time has no time zone")
            return MiddleMarker(
                started,
                str(data["origin_id"]),
                str(data["origin_address"]),
                tuple(MacAddress.parse(m) for m in data["bssids"]),
                tuple(str(t) for t in data.get("tested", ())),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def clear(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)
