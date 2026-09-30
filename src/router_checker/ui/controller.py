"""App logic between the widgets and the check engine (QtCore only).

The controller is the single owner of the settings, the history store and the
check engine. Everything that touches the network or the database runs in a
QThreadPool worker; results come back to the UI thread only through signals.

Test all and the Switch button are "runs": while one is active, regular checks,
the check timer and "check when the network changes" pause, and the run ends
with a regular check of wherever the PC is connected then.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, SignalInstance, Slot

from router_checker.core.auto_switch import Action, AutoSwitchPolicy, CheckView, Decision
from router_checker.core.checker import CheckEngine, CycleReport
from router_checker.core.errors import LocationPermissionError, WifiUnavailableError
from router_checker.core.export import write_checks_csv
from router_checker.core.middle import Endpoint
from router_checker.core.middle_switching import (
    MiddleMarkerFile,
    MiddleRunner,
    gather_middle_plan,
    plan_middle,
)
from router_checker.core.models import (
    CheckRecord,
    Event,
    GatewayInfo,
    Router,
    ScanEntry,
    ScanObservation,
    ScorePoint,
)
from router_checker.core.popularity import PopularTimes, popular_times
from router_checker.core.presentation import (
    Message,
    Status,
    StatusLevel,
    auto_switch_message,
    auto_switch_progress,
    auto_switch_reason,
    overall_status,
    progress_steps,
    progress_text,
    summarize_test_all,
    switch_summary,
)
from router_checker.core.protocols import (
    Clock,
    DnsService,
    HistoryStore,
    IdleMonitor,
    MiddleRouterClient,
    NetworkChangeWatcher,
    NetworkInfoService,
    PingService,
    RouterSwitcher,
    StartupService,
    WifiService,
)
from router_checker.core.scheduler import effective_interval, scheduled_test_all_due
from router_checker.core.series import RouterCharts, ScoreLine, max_gap, router_charts, score_lines
from router_checker.core.settings import Settings, save_settings
from router_checker.core.switching import (
    ON_ETHERNET,
    SWITCH_EVENT,
    TEST_ALL_EVENT,
    MarkerFile,
    Progress,
    RunnerBase,
    Stage,
    SwitchResult,
    SwitchRunner,
    SwitchTiming,
    TestAllPlan,
    TestAllResult,
    gather_plan,
    plan_test_all,
)

log = logging.getLogger(__name__)

SPARKLINE_SPAN = timedelta(hours=24)
NETWORK_SETTLE_MS = 5000  # wait for DHCP and ARP after a route change
POLL_MS = 1000
SCHEDULE_POLL_MS = 60_000  # how often to see whether a scheduled Test all is due
LATE_GRACE = timedelta(seconds=10)  # timer missed, e.g. after sleep
HISTORY_LIMIT = 200
EVENTS_LIMIT = 100
SCAN_REUSE = timedelta(minutes=10)  # plan with the last check's scan when it's this recent
MARKER_FILE = "test-all-restore.json"
MIDDLE_MARKER_FILE = "middle-restore.json"  # the same, behind the middle router
NO_MIDDLE_CLIENT = "Switching through the middle router isn't available here."
NO_SWITCHER = "Switching networks isn't available here."


@dataclass
class Services:
    wifi: WifiService
    ping: PingService
    dns: DnsService
    netinfo: NetworkInfoService
    clock: Clock
    watcher: NetworkChangeWatcher | None = None
    switcher: RouterSwitcher | None = None  # Test all and the Switch button
    idle: IdleMonitor | None = None  # scheduled Test all waits for the user to be away
    startup: StartupService | None = None  # Start with Windows
    has_wifi: bool = True  # False on a PC without a Wi-Fi adapter
    middle: MiddleRouterClient | None = None  # switching through your middle router


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the UI shows after one check."""

    report: CycleReport
    status: Status
    sparklines: dict[str, list[ScorePoint]]
    switchable: frozenset[str] = frozenset()  # routers the Switch button can connect to


@dataclass(frozen=True, slots=True)
class HistoryData:
    checks: list[CheckRecord]
    events: list[Event]


@dataclass(frozen=True, slots=True)
class RouterDetails:
    router_id: str
    last_check: CheckRecord | None
    last_seen: ScanObservation | None
    events: list[Event]


@dataclass(frozen=True, slots=True)
class ScanResult:
    entries: list[ScanEntry]
    profiles: list[str]


@dataclass(frozen=True, slots=True)
class ScoreHistory:
    start: datetime
    end: datetime
    lines: list[ScoreLine]


@dataclass(frozen=True, slots=True)
class RunState:
    """What the dashboard, tray and flyout show while a run is going on."""

    kind: str  # "test_all", "switch" or "recover"
    text: str  # e.g. "Testing Cafe (2 of 3)…"
    steps: tuple[int, int] | None = None  # (done, all) for Test all's progress bar
    can_cancel: bool = False


@dataclass
class _Run:
    """A Test all, a switch, or going back after an interrupted Test all."""

    kind: str  # "test_all", "switch" or "recover"
    name: str = ""  # the router a switch goes to
    scheduled: bool = False
    total: int = 0  # routers Test all switches to
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Progress | None = None
    outcome: TestAllResult | SwitchResult | None = None
    error: str | None = None
    finishing: bool = False  # the closing regular check runs
    decision: Decision | None = None  # set for an automatic switch
    from_id: str | None = None  # the router a switch leaves


def gateway_key(gateway: GatewayInfo | None) -> tuple[str, str, str] | None:
    """What changes when you move to another network, or plug in a cable."""
    if gateway is None:
        return None
    return gateway.kind.value, gateway.gateway_ip, str(gateway.gateway_mac)


def _error_text(exc: object) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


class _Task(QRunnable):
    def __init__(
        self,
        task_id: int,
        fn: Callable[[], Any],
        done: SignalInstance,
        failed: SignalInstance,
    ) -> None:
        super().__init__()
        self._id, self._fn, self._done, self._failed = task_id, fn, done, failed

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # handed to the UI thread
            self._failed.emit(self._id, exc)
        else:
            self._done.emit(self._id, result)


class AppController(QObject):
    checkStarted = Signal()
    checkFinished = Signal(object)  # Snapshot
    checkFailed = Signal(str)
    settingsChanged = Signal(object)  # Settings
    alertRaised = Signal(object)  # Alert
    locationStatus = Signal(object)  # True, False, or None if unknown
    activityChanged = Signal()  # a run started, progressed or ended: see ``activity``
    testAllFinished = Signal(object, bool)  # Message, scheduled
    switchFinished = Signal(object)  # Message
    autoSwitched = Signal(object)  # Message: an automatic switch (or going back) ended

    _taskDone = Signal(int, object)
    _taskFailed = Signal(int, object)
    _progress = Signal(object)  # Progress, emitted by the Test all worker

    def __init__(
        self,
        settings: Settings,
        settings_path: Path | None,
        store: HistoryStore,
        services: Services,
        parent: QObject | None = None,
        tz: tzinfo | None = None,  # local time for quiet hours; None: the computer's
        switch_timing: SwitchTiming | None = None,  # shorter waits in tests
    ) -> None:
        super().__init__(parent)
        self._tz = tz
        self._settings = settings
        self._settings_path = settings_path
        self._store = store
        self._services = services
        self._engine = CheckEngine(
            settings,
            wifi=services.wifi,
            ping=services.ping,
            dns=services.dns,
            netinfo=services.netinfo,
            store=store,
            clock=services.clock,
        )
        self._marker = MarkerFile(settings_path.parent / MARKER_FILE) if settings_path else None
        self._runner: SwitchRunner | None = None
        if services.switcher is not None:
            self._runner = SwitchRunner(
                self._engine,
                services.switcher,
                services.wifi,
                services.netinfo,
                store,
                services.clock,
                self._marker,
                switch_timing or SwitchTiming(),
            )
        self._middle_marker = (
            MiddleMarkerFile(settings_path.parent / MIDDLE_MARKER_FILE) if settings_path else None
        )
        self._middle_runner: MiddleRunner | None = None
        if services.middle is not None:
            self._middle_runner = MiddleRunner(
                self._engine,
                services.middle,
                services.netinfo,
                store,
                services.clock,
                self._middle_marker,
                switch_timing or SwitchTiming(),
            )
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._callbacks: dict[int, tuple[Callable[[Any], None], Callable[[Any], None] | None]] = {}
        self._ids = itertools.count(1)
        self._taskDone.connect(self._on_task_done)
        self._taskFailed.connect(self._on_task_failed)
        self._progress.connect(self._on_progress)

        self._checking = False
        self._check_again = False
        self._engine_stale = False
        self._stopped = False
        self._stop = threading.Event()  # tells a running check or scan to end early
        self._last_gateway: tuple[str, str] | None = None
        self._run: _Run | None = None
        self._worker_busy = False  # a run's worker uses the engine
        self._queued: tuple[TestAllPlan, bool] | None = None  # Test all waiting for a check
        self._planning_scheduled = False
        self._last_test_all: datetime | None = None
        self._policy = AutoSwitchPolicy()
        self.last_snapshot: Snapshot | None = None
        self.last_failure: str | None = None
        self.next_check_at: datetime | None = None
        self.network_settle_ms = NETWORK_SETTLE_MS

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.check_now)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._poll_tick)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._probe_gateway)
        self._schedule_timer = QTimer(self)
        self._schedule_timer.setInterval(SCHEDULE_POLL_MS)
        self._schedule_timer.timeout.connect(self.maybe_run_scheduled_test_all)

    # --- state ------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def is_checking(self) -> bool:
        return self._checking

    @property
    def is_busy(self) -> bool:
        """A check or a run is going on (or Test all waits for a check to end)."""
        return self._checking or self._run is not None or self._queued is not None

    @property
    def is_testing_all(self) -> bool:
        return self._run is not None and self._run.kind == "test_all"

    @property
    def can_switch(self) -> bool:
        """Test all and switching exist here: over Wi-Fi, or through a middle router."""
        middle = self._middle_runner is not None and self._settings.middle is not None
        return self._runner is not None or middle

    @property
    def behind_middle(self) -> bool:
        """The last check found the PC behind your middle router."""
        snap = self.last_snapshot
        return snap is not None and snap.report.middle is not None

    def _active_runner(self) -> RunnerBase | None:
        """What switches routers right now: the middle router when the PC is behind
        it, otherwise this PC's Wi-Fi (not on a cable)."""
        if self.behind_middle:
            return self._middle_runner
        return None if self.on_ethernet else self._runner

    def test_middle_router(self, endpoint: Endpoint, on_done: Callable[[str | None], None]) -> None:
        """Does the middle router answer? None if it does, else why not (worker thread)."""
        client = self._services.middle
        if client is None:
            on_done(NO_MIDDLE_CLIENT)
            return
        self.run_task(lambda: client.reachable(endpoint), on_done, lambda e: on_done(str(e)))

    @property
    def has_wifi(self) -> bool:
        return self._services.has_wifi

    def current_gateway(
        self,
        on_done: Callable[[GatewayInfo | None], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """The router you're connected to right now, Wi-Fi or Ethernet (worker thread)."""
        netinfo, choice = self._services.netinfo, self._settings.connection
        self.run_task(lambda: netinfo.gateway(choice), on_done, on_error)

    @property
    def can_start_with_windows(self) -> bool:
        return self._services.startup is not None

    def starts_with_windows(self) -> bool:
        """Read from Windows each time: Task Manager can turn it off too."""
        startup = self._services.startup
        if startup is None:
            return False
        try:
            return startup.enabled()
        except OSError:
            log.warning("could not read the startup entry", exc_info=True)
            return False

    def set_start_with_windows(self, on: bool) -> str | None:
        """Turn start with Windows on or off; an error message if Windows refused."""
        startup = self._services.startup
        if startup is None:
            return "Not available in this copy of the app."
        try:
            startup.set_enabled(on)
        except OSError as exc:
            log.warning("could not change the startup entry", exc_info=True)
            return f"Windows didn't allow the change ({exc.strerror or exc})."
        log.info("start with Windows %s", "on" if on else "off")
        return None

    @property
    def run_state(self) -> RunState | None:
        """The run going on, if any (a queued Test all counts)."""
        run = self._run
        if run is None:
            if self._queued is None:
                return None
            return RunState("test_all", "Test all starts after this check…", (0, 1), True)
        if run.kind == "switch":
            text = "Checking it…" if run.finishing else f"Switching to {run.name}…"
            return RunState("switch", text)
        if run.kind == "recover":
            return RunState("recover", "Going back to your network after an interrupted Test all…")
        done, steps = progress_steps(run.progress, run.total)
        restoring = run.progress is not None and run.progress.stage is Stage.RESTORING
        if run.finishing:
            return RunState("test_all", "Checking the network you're on…", (steps - 1, steps))
        if run.cancel.is_set():
            text = progress_text(run.progress) if restoring else "Cancelling…"
            return RunState("test_all", text, (done, steps))
        return RunState("test_all", progress_text(run.progress), (done, steps), can_cancel=True)

    @property
    def activity(self) -> str | None:
        """What a run is doing right now, e.g. "Testing Cafe (2 of 3)…"."""
        state = self.run_state
        return state.text if state else None

    @property
    def data_dir(self) -> Path | None:
        return self._settings_path.parent if self._settings_path else None

    def now(self) -> datetime:
        return self._services.clock.now()

    def local_time(self, when: datetime) -> datetime:
        return when.astimezone(self._tz)

    def alerts_allowed(self, when: datetime) -> bool:
        """Alerts are on and ``when`` is outside quiet hours."""
        s = self._settings
        return s.notifications_enabled and not s.quiet_hours.contains(self.local_time(when).time())

    def can_switch_to(self, router_id: str) -> bool:
        """The Switch button (or a notification's) may offer this router right now."""
        snap = self.last_snapshot
        return (
            self._active_runner() is not None
            and snap is not None
            and router_id in snap.switchable
            and not self.is_busy
        )

    @property
    def on_ethernet(self) -> bool:
        """The last check went over a cable, not through a middle router: Test all and
        switching are off."""
        snap = self.last_snapshot
        if snap is None or snap.report.gateway is None:
            return False
        return snap.report.gateway.wired and snap.report.middle is None

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._apply_watcher()
        self._poll.start()
        self._schedule_timer.start()
        store = self._store
        self.run_task(lambda: store.last_event(TEST_ALL_EVENT), self._loaded_last_test_all)
        self.run_task(lambda: store.last_event(SWITCH_EVENT), self._loaded_last_switch)
        if self._runner is not None and self._marker is not None and self._marker.path.exists():
            self._recover(self._runner)
        elif (
            self._middle_runner is not None
            and self._middle_marker is not None
            and self._middle_marker.path.exists()
        ):
            self._recover(self._middle_runner)
        else:
            self.check_now()

    def shutdown(self, wait_ms: int = 15000) -> None:
        """Stop timers, end a running check early (nothing from it is saved) and wait
        up to ``wait_ms`` for the workers; that usually takes well under a second.
        A running Test all only asks Windows to reconnect before it ends."""
        self._stopped = True
        self._stop.set()
        if self._run is not None:
            self._run.cancel.set()
        for timer in (self._timer, self._poll, self._settle, self._schedule_timer):
            timer.stop()
        self._stop_watcher()
        self._pool.clear()
        self._pool.waitForDone(wait_ms)

    # --- checks -----------------------------------------------------------------

    @Slot()
    def check_now(self) -> None:
        if self._stopped:
            return
        if self._run is not None:
            log.info("check skipped: %s is running and ends with a check", self._run.kind)
            return
        if self._checking:
            self._check_again = True
            return
        self._start_check()

    def _start_check(self) -> None:
        self._timer.stop()
        self._sync_engine()
        self._checking = True
        self.checkStarted.emit()
        self.run_task(self._run_cycle, self._on_cycle_done, self._on_cycle_failed)

    def _run_cycle(self) -> tuple[CycleReport, dict[str, list[ScorePoint]], frozenset[str]]:
        """Runs in a worker thread."""
        report = self._engine.run_cycle(stop=self._stop)
        since = report.timestamp - SPARKLINE_SPAN
        sparklines = {r.id: self._store.scores(r.id, since) for r in report.settings.routers}
        return report, sparklines, self._switchable(report)

    def _switchable(self, report: CycleReport) -> frozenset[str]:
        """Routers Test all could switch to right now (worker thread)."""
        if report.middle is not None:
            if self._middle_runner is None:
                return frozenset()
            observed = {}
            for router in report.settings.routers:
                observation = self._store.latest_observation(router.id)
                if observation is not None:
                    observed[router.id] = observation.entry.bssid
            plan = plan_middle(report.settings.routers, report.upstream_ip, report.scan, observed)
            return (
                frozenset(c.router.id for c in plan.to_test)
                if plan.blocker is None
                else frozenset()
            )
        switcher = self._services.switcher
        if switcher is None or report.scan is None:
            return frozenset()
        try:
            saved = switcher.saved_networks()
        except (LocationPermissionError, WifiUnavailableError, OSError):
            log.warning("could not read the saved Wi-Fi profiles", exc_info=True)
            return frozenset()
        plan = plan_test_all(
            report.settings.routers, report.connection, report.gateway, report.scan, saved
        )
        return frozenset(c.router.id for c in plan.to_test) if plan.blocker is None else frozenset()

    def _on_cycle_done(
        self, result: tuple[CycleReport, dict[str, list[ScorePoint]], frozenset[str]]
    ) -> None:
        report, sparklines, switchable = result
        self._checking = False
        self.last_failure = None
        if report.linked:
            self._set_settings(self._settings.with_linked_macs(report.linked))
        self._sync_engine()
        self._last_gateway = gateway_key(report.gateway)
        snapshot = Snapshot(report, overall_status(report), sparklines, switchable)
        self.last_snapshot = snapshot
        name = report.match.router.name if report.match.router else "-"
        log.info(
            "check done: %s (%s), router %s", snapshot.status.title, snapshot.status.detail, name
        )
        self._schedule(report.timestamp, report.unstable)
        if report.alert is not None:
            if self.alerts_allowed(report.timestamp):
                self.alertRaised.emit(report.alert)
            else:
                log.info("alert not shown (alerts off or quiet hours): %s", report.alert.title)
        self.locationStatus.emit(None if report.wifi_error else report.location_allowed)
        self.checkFinished.emit(snapshot)
        self._after_check(report)

    def _on_cycle_failed(self, exc: BaseException) -> None:
        self._checking = False
        message = _error_text(exc)
        log.error("check failed: %s", message, exc_info=exc)
        self.last_failure = message
        self._schedule(self.now(), unstable=False)
        self.checkFailed.emit(message)
        self._after_check(None)

    def _after_check(self, report: CycleReport | None) -> None:
        if self._run is not None and self._run.finishing:
            self._end_run(report)
        if self._queued is not None:
            plan, scheduled = self._queued
            self._queued = None
            if not self.start_test_all(plan, scheduled):
                self.activityChanged.emit()
        if report is not None:
            self._consider_switching(report)
        if self._check_again:
            self._check_again = False
            QTimer.singleShot(0, self.check_now)

    def _schedule(self, last_check: datetime, unstable: bool) -> None:
        if self._stopped:
            return
        due = last_check + effective_interval(self._settings.interval_min, unstable)
        self.next_check_at = due
        self._timer.start(max(0, round((due - self.now()).total_seconds() * 1000)))

    def _sync_engine(self) -> None:
        if self._engine_stale and not self._checking and not self._worker_busy:
            self._engine.settings = self._settings
            self._engine_stale = False

    # --- Test all, switching and recovering -------------------------------------------

    def plan_test_all(
        self,
        on_done: Callable[[TestAllPlan], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        """Work out what Test all would do (worker thread; a recent scan is reused)."""
        if self.behind_middle:
            if self._middle_runner is None:
                on_done(TestAllPlan(None, None, blocker=NO_MIDDLE_CLIENT))
                return
            engine, netinfo, stop = self._engine, self._services.netinfo, self._stop
            wifi = self._services.wifi if self.has_wifi else None
            store, scan = self._store, self._recent_scan()
            self.run_task(
                lambda: gather_middle_plan(engine, netinfo, wifi, store, scan, stop),
                on_done,
                on_error,
            )
            return
        switcher = self._services.switcher
        if switcher is None:
            on_done(TestAllPlan(None, None, blocker=NO_SWITCHER))
            return
        if self.on_ethernet:
            on_done(TestAllPlan(None, None, blocker=ON_ETHERNET))
            return
        wifi, netinfo, stop = self._services.wifi, self._services.netinfo, self._stop
        routers, scan = self._settings.routers, self._recent_scan()
        self.run_task(
            lambda: gather_plan(routers, wifi, netinfo, switcher, scan, stop), on_done, on_error
        )

    def start_test_all(self, plan: TestAllPlan, scheduled: bool = False) -> bool:
        """Start Test all with a plan from ``plan_test_all``. During a check it starts
        once the check ends. False if it can't run (busy with another run, or the plan
        has nothing to test)."""
        runner = self._middle_runner if plan.via_middle else self._runner
        if self._stopped or runner is None or not plan.can_run:
            return False
        if self._run is not None or self._queued is not None:
            return False
        if self._checking:
            self._queued = (plan, scheduled)
            self.activityChanged.emit()
            return True
        run = _Run("test_all", scheduled=scheduled, total=len(plan.to_test))
        exiting = self._stop
        log.info("Test all starts%s: %d routers", " (scheduled)" if scheduled else "", run.total)
        self._begin(run)
        self.run_task(
            lambda: runner.test_all(
                plan,
                cancel=run.cancel,
                exiting=exiting,
                progress=self._progress.emit,
                scheduled=scheduled,
            ),
            self._on_run_done,
            self._on_run_failed,
        )
        return True

    def cancel_test_all(self) -> None:
        if self._queued is not None:
            self._queued = None
            self.activityChanged.emit()
        run = self._run
        if run is not None and run.kind == "test_all" and not run.cancel.is_set():
            log.info("Test all cancelled")
            run.cancel.set()
            self.activityChanged.emit()

    def switch_to(self, router_id: str) -> bool:
        """Connect to another router (the Switch button); back to where you were if that
        fails. Ends with a check. False if it can't start now."""
        router = self._settings.router(router_id)
        if router is None or self._stopped or self._active_runner() is None or self.is_busy:
            return False
        self._start_switch(router, None)
        return True

    def _start_switch(self, router: Router, decision: Decision | None) -> None:
        switcher, runner, stop = self._services.switcher, self._runner, self._stop
        behind = self.behind_middle
        wifi, netinfo = self._services.wifi, self._services.netinfo
        routers, scan = self._settings.routers, self._recent_scan()
        names = {r.id: r.name for r in routers}
        reason = auto_switch_reason(decision, names) if decision else None
        snap = self.last_snapshot
        current = snap.report.match.router if snap else None

        middle, engine, store = self._middle_runner, self._engine, self._store
        wifi_for_scan = wifi if self.has_wifi else None

        def work() -> SwitchResult:
            if behind:
                assert middle is not None
                plan = gather_middle_plan(engine, netinfo, wifi_for_scan, store, scan, stop)
                return middle.switch(plan, router, stop=stop, reason=reason)
            assert runner is not None
            plan = gather_plan(routers, wifi, netinfo, switcher, scan, stop)
            return runner.switch(plan, router, stop=stop, reason=reason)

        log.info("switching to %s%s", router.name, f" automatically: {reason}" if reason else "")
        run = _Run("switch", name=router.name, decision=decision)
        run.from_id = current.id if current else None
        self._begin(run)
        self.run_task(work, self._on_run_done, self._on_run_failed)

    def _recover(self, runner: SwitchRunner | MiddleRunner) -> None:
        stop = self._stop
        self._begin(_Run("recover"))
        self.run_task(lambda: runner.recover(stop=stop), self._on_recovered, self._on_recovered)

    def _on_recovered(self, result: object) -> None:
        self._worker_busy = False
        self._run = None
        if isinstance(result, BaseException):
            log.error("going back after an interrupted Test all failed", exc_info=result)
        elif result is not None:
            log.info("%s", result.message)
        self.activityChanged.emit()
        self.check_now()

    def _recent_scan(self) -> list[ScanEntry] | None:
        snap = self.last_snapshot
        if snap is None or snap.report.scan is None:
            return None
        if self.now() - snap.report.timestamp > SCAN_REUSE:
            return None
        return snap.report.scan

    def _begin(self, run: _Run) -> None:
        self._run = run
        self._worker_busy = True
        self._timer.stop()
        self._settle.stop()
        self.next_check_at = None
        self.activityChanged.emit()

    @Slot(object)
    def _on_progress(self, progress: Progress) -> None:
        if self._run is not None:
            self._run.progress = progress
            self.activityChanged.emit()

    def _on_run_done(self, outcome: TestAllResult | SwitchResult) -> None:
        run = self._run
        if run is None:
            return
        self._worker_busy = False
        run.outcome = outcome
        if isinstance(outcome, TestAllResult):
            self._last_test_all = self.now()
            if outcome.linked:
                self._set_settings(self._settings.with_linked_macs(outcome.linked))
        elif isinstance(outcome, SwitchResult) and outcome.linked is not None:
            self._set_settings(self._settings.with_linked_macs([outcome.linked]))
        self._finish_with_check()

    def _on_run_failed(self, exc: BaseException) -> None:
        run = self._run
        if run is None:
            return
        self._worker_busy = False
        run.error = _error_text(exc)
        log.error("%s failed: %s", run.kind, run.error, exc_info=exc)
        if run.kind == "test_all":
            self._last_test_all = self.now()
        self._finish_with_check()

    def _finish_with_check(self) -> None:
        assert self._run is not None
        self._run.finishing = True
        self.activityChanged.emit()
        self._start_check()

    def _end_run(self, report: CycleReport | None) -> None:
        run, self._run = self._run, None
        assert run is not None
        self.activityChanged.emit()
        if run.kind == "test_all":
            result = run.outcome if isinstance(run.outcome, TestAllResult) else None
            message = summarize_test_all(result, report, run.error)
            log.info("Test all done: %s. %s", message.title, message.text)
            self.testAllFinished.emit(message, run.scheduled)
            return
        result = run.outcome if isinstance(run.outcome, SwitchResult) else None
        decision, now = run.decision, self.now()
        if result is not None and result.ok:
            automatic = decision is not None and decision.action is Action.SWITCH
            self._policy.switched(run.from_id, result.router.id, now, automatic)
        elif result is not None and (result.restored is not None or decision is not None):
            self._policy.switch_failed(result.router.id, now)
        if result is None:
            message = Message(StatusLevel.BAD, f"Couldn't switch to {run.name}", f"{run.error}.")
        elif decision is not None and result.ok:
            message = auto_switch_message(decision, {r.id: r.name for r in self._settings.routers})
        else:
            message = switch_summary(result)
        log.info("switch done: %s. %s", message.title, message.text)
        if decision is not None:
            self.autoSwitched.emit(message)
        else:
            self.switchFinished.emit(message)

    # --- automatic switching -----------------------------------------------------------

    @property
    def auto_switch_text(self) -> str | None:
        """The dashboard's line about automatic switching, when it's on."""
        if not self._settings.auto_switch or not self.can_switch:
            return None
        if self.on_ethernet:
            return "Automatic switching is paused while you're on Ethernet."
        names = {r.id: r.name for r in self._settings.routers}
        return auto_switch_progress(self._policy.progress(self.now()), names)

    def _loaded_last_switch(self, event: Event | None) -> None:
        if event is not None:
            self._policy.restore_last_switch(event.timestamp)

    def _consider_switching(self, report: CycleReport) -> None:
        """Let the policy see this check; switch if it says so and switching is on."""
        snap = self.last_snapshot
        switchable = snap.switchable if snap is not None else frozenset()
        current = report.match.router if report.record is not None else None
        if report.gateway is not None and report.gateway.wired and report.middle is None:
            current = None  # nothing to switch on a cable; the streak starts over after it
        candidates = {
            s.router.id: s.score
            for s in report.statuses
            if s.score is not None
            and s.router.id in switchable
            and (current is None or s.router.id != current.id)
        }
        decision = self._policy.observe(
            CheckView(
                report.timestamp,
                current.id if current else None,
                report.record.verdict if report.record else None,
                report.recommendation,
                candidates,
            )
        )
        if decision.action is Action.STAY:
            return
        router = self._settings.router(decision.router_id or "")
        if not self._settings.auto_switch or router is None:
            return
        if self._stopped or self._active_runner() is None or self.is_busy:
            log.info("automatic switch to %s skipped: busy", router.name)
            return
        self._start_switch(router, decision)

    # --- scheduled Test all ------------------------------------------------------------

    def _loaded_last_test_all(self, event: Event | None) -> None:
        if event is not None and self._last_test_all is None:
            self._last_test_all = event.timestamp

    @Slot()
    def maybe_run_scheduled_test_all(self) -> None:
        """Start a scheduled Test all if it's on, due, and you've been away 5 minutes."""
        s = self._settings
        idle_monitor = self._services.idle
        if (
            not s.scheduled_test_all
            or self._stopped
            or self._active_runner() is None
            or idle_monitor is None
        ):
            return
        if self.is_busy or self._planning_scheduled:
            return
        try:
            idle = timedelta(seconds=idle_monitor.idle_seconds())
        except OSError:
            log.warning("could not read the idle time", exc_info=True)
            return
        if not scheduled_test_all_due(self._last_test_all, self.now(), s.test_all_interval_h, idle):
            return
        self._planning_scheduled = True
        self.plan_test_all(self._start_scheduled, self._scheduled_plan_failed)

    def _start_scheduled(self, plan: TestAllPlan) -> None:
        self._planning_scheduled = False
        if plan.origin is None or not plan.can_run:
            reason = plan.blocker or ("not on Wi-Fi" if plan.origin is None else "nothing to test")
            log.info("scheduled Test all skipped: %s", reason)
            self._last_test_all = self.now()  # look again after the interval, not every minute
            return
        if not self.start_test_all(plan, scheduled=True):
            log.info("scheduled Test all postponed: busy")

    def _scheduled_plan_failed(self, exc: BaseException) -> None:
        self._planning_scheduled = False
        self._last_test_all = self.now()
        log.error("planning the scheduled Test all failed", exc_info=exc)

    # --- network changes --------------------------------------------------------

    def _apply_watcher(self) -> None:
        watcher = self._services.watcher
        if watcher is None:
            return
        if self._settings.check_on_network_change and not self._stopped:
            try:
                watcher.start()
            except OSError:
                log.warning("could not watch for network changes", exc_info=True)
        else:
            self._stop_watcher()

    def _stop_watcher(self) -> None:
        if self._services.watcher is not None:
            try:
                self._services.watcher.stop()
            except OSError:
                log.warning("could not stop the network watcher", exc_info=True)
        self._settle.stop()

    def _poll_tick(self) -> None:
        watcher = self._services.watcher
        changed = watcher is not None and watcher.take_change()
        if changed and self._settings.check_on_network_change and self._run is None:
            self._settle.start(self.network_settle_ms)  # restarts on every burst
        due = self.next_check_at
        idle = not self._checking and self._run is None
        if idle and due is not None and self.now() > due + LATE_GRACE:
            log.info("the check timer was late (sleep?); checking now")
            self.check_now()

    def _probe_gateway(self) -> None:
        netinfo, choice = self._services.netinfo, self._settings.connection
        self.run_task(lambda: netinfo.gateway(choice), self._on_gateway_probed, self._log_error)

    def _on_gateway_probed(self, gateway: GatewayInfo | None) -> None:
        key = gateway_key(gateway)
        if key != self._last_gateway and self._run is None:
            log.info("network changed (%s -> %s); checking now", self._last_gateway, key)
            self._last_gateway = key
            self.check_now()

    # --- settings and routers ---------------------------------------------------

    def update_settings(self, settings: Settings) -> None:
        self._set_settings(settings)

    def add_router(self, router: Router) -> None:
        self._set_settings(self._settings.with_router(router))
        self.check_now()

    def update_router(self, router: Router) -> None:
        old = self._settings.router(router.id)
        self._set_settings(self._settings.with_router(router))
        if old is None or old.ssid != router.ssid or set(old.macs) != set(router.macs):
            self.check_now()

    def remove_router(self, router_id: str) -> None:
        self._set_settings(self._settings.without_router(router_id))

    def complete_first_run(self, interval_min: int) -> None:
        self._set_settings(replace(self._settings, interval_min=interval_min, first_run_done=True))

    def _set_settings(self, settings: Settings) -> None:
        old = self._settings
        self._settings = settings
        if self._settings_path is not None:
            try:
                save_settings(self._settings_path, settings)
            except OSError:
                log.error("could not save settings", exc_info=True)
        self._engine_stale = True
        self._sync_engine()
        if settings.check_on_network_change != old.check_on_network_change:
            self._apply_watcher()
        last = self.last_snapshot
        idle = not self._checking and self._run is None
        if settings.interval_min != old.interval_min and last and idle:
            self._schedule(last.report.timestamp, last.report.unstable)
        self.settingsChanged.emit(settings)

    # --- background reads for the UI --------------------------------------------

    def probe_location(self) -> None:
        self.run_task(
            self._services.wifi.location_allowed,
            self.locationStatus.emit,
            lambda _exc: self.locationStatus.emit(None),
        )

    def scan_networks(
        self, on_done: Callable[[ScanResult], None], on_error: Callable[[BaseException], None]
    ) -> None:
        wifi, stop = self._services.wifi, self._stop

        def work() -> ScanResult:
            entries = wifi.scan(stop=stop)
            try:
                profiles = wifi.saved_profiles()
            except Exception:
                profiles = []
            return ScanResult(entries, profiles)

        self.run_task(work, on_done, on_error)

    def load_history(self, router_id: str | None, on_done: Callable[[HistoryData], None]) -> None:
        store = self._store

        def work() -> HistoryData:
            return HistoryData(
                store.recent_checks(HISTORY_LIMIT, router_id), store.events(router_id, EVENTS_LIMIT)
            )

        self.run_task(work, on_done, self._log_error)

    def load_router_details(self, router_id: str, on_done: Callable[[RouterDetails], None]) -> None:
        store = self._store

        def work() -> RouterDetails:
            checks = store.recent_checks(1, router_id)
            return RouterDetails(
                router_id,
                checks[0] if checks else None,
                store.latest_observation(router_id),
                store.events(router_id, EVENTS_LIMIT),
            )

        self.run_task(work, on_done, self._log_error)

    def load_router_charts(
        self, router_id: str, span: timedelta, on_done: Callable[[RouterCharts], None]
    ) -> None:
        store, end = self._store, self.now()
        start, gap = end - span, max_gap(self._settings.interval_min)

        def work() -> RouterCharts:
            checks, scores = store.checks(router_id, start), store.scores(router_id, start)
            return router_charts(router_id, checks, scores, start, end, gap)

        self.run_task(work, on_done, self._log_error)

    def load_popular_times(self, router_id: str, on_done: Callable[[PopularTimes], None]) -> None:
        """Busyness by weekday and hour over the kept history, in local time."""
        store, tz = self._store, self._tz
        since = self.now() - timedelta(days=self._settings.retention_days)

        def work() -> PopularTimes:
            return popular_times(store.hourly(router_id, since), tz)

        self.run_task(work, on_done, self._log_error)

    def load_score_lines(self, span: timedelta, on_done: Callable[[ScoreHistory], None]) -> None:
        """Every router's score over ``span``, for the History comparison chart."""
        store, end = self._store, self.now()
        start, gap = end - span, max_gap(self._settings.interval_min)
        router_ids = [r.id for r in self._settings.routers]

        def work() -> ScoreHistory:
            scores = {rid: store.scores(rid, start) for rid in router_ids}
            return ScoreHistory(start, end, score_lines(scores, start, end, gap))

        self.run_task(work, on_done, self._log_error)

    def export_checks(
        self,
        path: Path,
        on_done: Callable[[int], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        """Write every kept check to ``path`` as CSV; ``on_done`` gets the row count."""
        store, tz = self._store, self._tz
        names = {r.id: r.name for r in self._settings.routers}

        def work() -> int:
            records = store.checks_since()
            with path.open("w", encoding="utf-8-sig", newline="") as stream:
                return write_checks_csv(stream, records, names, tz)

        self.run_task(work, on_done, on_error)

    # --- worker plumbing --------------------------------------------------------

    def run_task(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None] | None = None,
    ) -> int:
        """Run ``fn`` in the pool; the callbacks run on the UI thread."""
        task_id = next(self._ids)
        self._callbacks[task_id] = (on_done, on_error)
        self._pool.start(_Task(task_id, fn, self._taskDone, self._taskFailed))
        return task_id

    @Slot(int, object)
    def _on_task_done(self, task_id: int, result: object) -> None:
        callbacks = self._callbacks.pop(task_id, None)
        if callbacks is not None and not self._stopped:
            self._call(callbacks[0], result)

    @Slot(int, object)
    def _on_task_failed(self, task_id: int, exc: object) -> None:
        callbacks = self._callbacks.pop(task_id, None)
        if callbacks is None or self._stopped:
            return
        if callbacks[1] is not None:
            self._call(callbacks[1], exc)
        else:
            self._log_error(exc)

    @staticmethod
    def _call(fn: Callable[[Any], None], arg: object) -> None:
        try:
            fn(arg)
        except RuntimeError as exc:
            if "already deleted" not in str(exc):
                raise
            log.debug("a window closed before its data arrived")

    @staticmethod
    def _log_error(exc: object) -> None:
        log.error(
            "background task failed: %s",
            exc,
            exc_info=exc if isinstance(exc, BaseException) else None,
        )
