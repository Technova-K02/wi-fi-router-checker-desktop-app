"""App logic between the widgets and the check engine (QtCore only).

The controller is the single owner of the settings, the history store and the
check engine. Everything that touches the network or the database runs in a
QThreadPool worker; results come back to the UI thread only through signals.
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, SignalInstance, Slot

from router_checker.core.checker import CheckEngine, CycleReport
from router_checker.core.export import write_checks_csv
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
from router_checker.core.presentation import Status, overall_status
from router_checker.core.protocols import (
    Clock,
    DnsService,
    HistoryStore,
    NetworkChangeWatcher,
    NetworkInfoService,
    PingService,
    WifiService,
)
from router_checker.core.scheduler import effective_interval
from router_checker.core.series import RouterCharts, ScoreLine, max_gap, router_charts, score_lines
from router_checker.core.settings import Settings, save_settings

log = logging.getLogger(__name__)

SPARKLINE_SPAN = timedelta(hours=24)
NETWORK_SETTLE_MS = 5000  # wait for DHCP and ARP after a route change
POLL_MS = 1000
LATE_GRACE = timedelta(seconds=10)  # timer missed, e.g. after sleep
HISTORY_LIMIT = 200
EVENTS_LIMIT = 100


@dataclass
class Services:
    wifi: WifiService
    ping: PingService
    dns: DnsService
    netinfo: NetworkInfoService
    clock: Clock
    watcher: NetworkChangeWatcher | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the UI shows after one check."""

    report: CycleReport
    status: Status
    sparklines: dict[str, list[ScorePoint]]


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


def gateway_key(gateway: GatewayInfo | None) -> tuple[str, str] | None:
    if gateway is None:
        return None
    return gateway.gateway_ip, str(gateway.gateway_mac)


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

    _taskDone = Signal(int, object)
    _taskFailed = Signal(int, object)

    def __init__(
        self,
        settings: Settings,
        settings_path: Path | None,
        store: HistoryStore,
        services: Services,
        parent: QObject | None = None,
        tz: tzinfo | None = None,  # local time for quiet hours; None: the computer's
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
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(4)
        self._callbacks: dict[int, tuple[Callable[[Any], None], Callable[[Any], None] | None]] = {}
        self._ids = itertools.count(1)
        self._taskDone.connect(self._on_task_done)
        self._taskFailed.connect(self._on_task_failed)

        self._checking = False
        self._check_again = False
        self._engine_stale = False
        self._stopped = False
        self._stop = threading.Event()  # tells a running check or scan to end early
        self._last_gateway: tuple[str, str] | None = None
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

    # --- state ------------------------------------------------------------------

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def is_checking(self) -> bool:
        return self._checking

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

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._apply_watcher()
        self._poll.start()
        self.check_now()

    def shutdown(self, wait_ms: int = 15000) -> None:
        """Stop timers, end a running check early (nothing from it is saved) and wait
        up to ``wait_ms`` for the workers; that usually takes well under a second."""
        self._stopped = True
        self._stop.set()
        for timer in (self._timer, self._poll, self._settle):
            timer.stop()
        self._stop_watcher()
        self._pool.clear()
        self._pool.waitForDone(wait_ms)

    # --- checks -----------------------------------------------------------------

    @Slot()
    def check_now(self) -> None:
        if self._stopped:
            return
        if self._checking:
            self._check_again = True
            return
        self._timer.stop()
        self._sync_engine()
        self._checking = True
        self.checkStarted.emit()
        self.run_task(self._run_cycle, self._on_cycle_done, self._on_cycle_failed)

    def _run_cycle(self) -> tuple[CycleReport, dict[str, list[ScorePoint]]]:
        """Runs in a worker thread."""
        report = self._engine.run_cycle(stop=self._stop)
        since = report.timestamp - SPARKLINE_SPAN
        sparklines = {r.id: self._store.scores(r.id, since) for r in report.settings.routers}
        return report, sparklines

    def _on_cycle_done(self, result: tuple[CycleReport, dict[str, list[ScorePoint]]]) -> None:
        report, sparklines = result
        self._checking = False
        self.last_failure = None
        if report.linked:
            self._set_settings(self._settings.with_linked_macs(report.linked))
        self._sync_engine()
        self._last_gateway = gateway_key(report.gateway)
        snapshot = Snapshot(report, overall_status(report), sparklines)
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
        self._maybe_check_again()

    def _on_cycle_failed(self, exc: BaseException) -> None:
        self._checking = False
        message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        log.error("check failed: %s", message, exc_info=exc)
        self.last_failure = message
        self._schedule(self.now(), unstable=False)
        self.checkFailed.emit(message)
        self._maybe_check_again()

    def _maybe_check_again(self) -> None:
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
        if self._engine_stale and not self._checking:
            self._engine.settings = self._settings
            self._engine_stale = False

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
        if self._settings.check_on_network_change and watcher is not None and watcher.take_change():
            self._settle.start(self.network_settle_ms)  # restarts on every burst
        due = self.next_check_at
        if not self._checking and due is not None and self.now() > due + LATE_GRACE:
            log.info("the check timer was late (sleep?); checking now")
            self.check_now()

    def _probe_gateway(self) -> None:
        self.run_task(self._services.netinfo.wifi_gateway, self._on_gateway_probed, self._log_error)

    def _on_gateway_probed(self, gateway: GatewayInfo | None) -> None:
        key = gateway_key(gateway)
        if key != self._last_gateway:
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
        if settings.interval_min != old.interval_min and last and not self._checking:
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
