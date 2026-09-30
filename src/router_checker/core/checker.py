"""One check cycle: identify the current router, run the full test on it,
scan passively for the others, then score, alert and recommend."""

from __future__ import annotations

import ipaddress
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from router_checker.core.alerts import AlertEngine
from router_checker.core.errors import (
    CheckCancelled,
    LocationPermissionError,
    WifiUnavailableError,
)
from router_checker.core.matching import Match, identify_current, router_state
from router_checker.core.measurements import to_record
from router_checker.core.models import (
    Alert,
    CheckRecord,
    Event,
    FullTestResult,
    GatewayInfo,
    Recommendation,
    Router,
    RouterStatus,
    ScanEntry,
    ScanObservation,
    Score,
    TargetResult,
    Verdict,
    WifiConnection,
)
from router_checker.core.popularity import busyness
from router_checker.core.protocols import (
    Clock,
    DnsService,
    HistoryStore,
    NetworkInfoService,
    NotificationService,
    PingService,
    WifiService,
)
from router_checker.core.recommendation import recommend
from router_checker.core.scheduler import next_check_at
from router_checker.core.scoring import (
    WINDOW,
    check_score,
    estimated_score,
    make_score,
    rolling_score,
)
from router_checker.core.settings import Settings
from router_checker.core.stats import summarize
from router_checker.core.wifi_info import best_entry_for, quality_to_rssi, same_channel_count

HISTORY_SPAN = timedelta(days=7)
PURGE_EVERY = timedelta(hours=6)


def _raise_if_stopped(stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise CheckCancelled("the check was stopped")


@dataclass(slots=True)
class CycleReport:
    timestamp: datetime
    settings: Settings  # includes MACs linked during this cycle
    location_allowed: bool
    wifi_error: str | None
    gateway: GatewayInfo | None
    connection: WifiConnection | None
    match: Match
    test: FullTestResult | None
    record: CheckRecord | None
    scan: list[ScanEntry] | None
    statuses: list[RouterStatus]
    alert: Alert | None
    recommendation: Recommendation | None
    unstable: bool
    next_check_at: datetime
    linked: list[Router] = field(default_factory=list)
    confirmed_unstable: bool = False  # unstable for `unstable_checks` checks in a row
    recovering: bool = False  # stable again, "back to normal" not confirmed yet

    @property
    def current(self) -> RouterStatus | None:
        return next((s for s in self.statuses if s.is_current), None)


class CheckEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        wifi: WifiService,
        ping: PingService,
        dns: DnsService,
        netinfo: NetworkInfoService,
        store: HistoryStore,
        clock: Clock,
        notifier: NotificationService | None = None,
    ) -> None:
        self._wifi = wifi
        self._ping = ping
        self._dns = dns
        self._netinfo = netinfo
        self._store = store
        self._clock = clock
        self._notifier = notifier
        self._alerts = AlertEngine()
        self._last_purge: datetime | None = None
        self.settings = settings

    @property
    def settings(self) -> Settings:
        return self._settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._settings = value
        self._alerts.unstable_checks = value.unstable_checks
        self._alerts.recovery_checks = value.recovery_checks
        self._alerts.cooldown = timedelta(minutes=value.alert_cooldown_min)

    # --- full test ----------------------------------------------------------

    def _test_target(self, target: str, stop: threading.Event | None) -> TargetResult:
        s = self._settings
        if stop is not None and stop.is_set():
            return TargetResult(target, None, None, None)  # discarded: full_test raises
        try:
            ipaddress.ip_address(target)
        except ValueError:
            dns = self._dns.resolve(target)
            if not dns.ok:
                return TargetResult(target, None, None, dns)
            address = dns.addresses[0]
        else:
            dns, address = None, target
        try:
            samples = self._ping.ping(
                address, s.pings_per_target, s.ping_timeout_ms, s.ping_spacing_ms, stop=stop
            )
        except (OSError, ValueError):  # e.g. an address the ping service can't handle
            samples = [None] * s.pings_per_target
        return TargetResult(target, address, summarize(samples), dns)

    def full_test(
        self,
        gateway: GatewayInfo,
        router: Router | None,
        connection: WifiConnection | None,
        rssi: int | None,
        timestamp: datetime | None = None,
        stop: threading.Event | None = None,
    ) -> FullTestResult:
        """Ping the gateway and every target at the same time.

        Raises CheckCancelled if ``stop`` was set meanwhile (the pings end early then).
        """
        s = self._settings
        now = timestamp or self._clock.now()
        with ThreadPoolExecutor(max_workers=1 + len(s.targets)) as pool:
            gw_future = pool.submit(
                self._ping.ping,
                gateway.gateway_ip,
                s.pings_per_target,
                s.ping_timeout_ms,
                s.ping_spacing_ms,
                stop=stop,
            )
            target_futures = [pool.submit(self._test_target, t, stop) for t in s.targets]
            gateway_ping = summarize(gw_future.result())
            targets = tuple(f.result() for f in target_futures)
        _raise_if_stopped(stop)
        return FullTestResult(
            timestamp=now,
            router_id=router.id if router else None,
            ssid=connection.ssid if connection else None,
            bssid=connection.bssid if connection else None,
            gateway=gateway,
            gateway_ping=gateway_ping,
            targets=targets,
            rssi=rssi,
            signal_quality=connection.signal_quality if connection else None,
        )

    # --- cycle --------------------------------------------------------------

    def run_cycle(self, stop: threading.Event | None = None) -> CycleReport:
        """Run one check. Once ``stop`` is set it raises CheckCancelled and saves nothing."""
        now = self._clock.now()
        s = self._settings
        location_allowed = True
        wifi_error: str | None = None

        gateway = self._netinfo.wifi_gateway()

        connection: WifiConnection | None = None
        scan: list[ScanEntry] | None = None
        try:
            connection = self._wifi.current_connection()
            scan = self._wifi.scan(stop=stop)
        except LocationPermissionError:
            location_allowed = False
        except WifiUnavailableError as exc:
            wifi_error = str(exc)

        # Which router are we on? Full test on the current connection.
        match = identify_current(s.routers, connection, gateway)
        current = match.router
        test: FullTestResult | None = None
        if gateway is not None:
            rssi = self._current_rssi(connection, scan)
            test = self.full_test(gateway, current, connection, rssi, now, stop)
        _raise_if_stopped(stop)  # nothing has been saved up to here

        # Link newly learned MACs to the current router.
        linked: list[Router] = []
        if current is not None and match.macs_to_link:
            current = current.with_macs(*match.macs_to_link)
            s = s.with_router(current)
            self.settings = s
            linked.append(current)
            macs = ", ".join(str(m) for m in match.macs_to_link)
            self._store.add_event(
                Event(now, current.id, "linked", f"Linked {macs} to {current.name}")
            )

        record: CheckRecord | None = None
        if test is not None:
            record = to_record(test, s.thresholds, s.pings_per_target)
            record = replace(record, score=check_score(record))
            self._store.add_check(record)

        # Passive observations of every router in the scan.
        observations: dict[str, ScanObservation] = {}
        if scan is not None:
            for router in s.routers:
                entry = best_entry_for(router, scan)
                if entry is None:
                    continue
                crowd = same_channel_count(entry, scan, router.macs)
                jitter = self._recent_jitter(router.id, now)
                busy = busyness(entry.bss_load, crowd, jitter)
                if busy is not None:
                    observations[router.id] = ScanObservation(now, router.id, entry, crowd, busy)
            self._store.add_observations(list(observations.values()))

        # Scores and states.
        statuses: list[RouterStatus] = []
        scored: dict[str, tuple[Score, datetime]] = {}
        answered = record is not None and not (
            record.gateway_loss_pct == 100 and not record.gateway_silent
        )
        for router in s.routers:
            obs = observations.get(router.id)
            score, data_time = self._score_for(router, obs, now)
            if score is not None and data_time is not None:
                scored[router.id] = (score, data_time)
            is_current = current is not None and router.id == current.id
            statuses.append(
                RouterStatus(
                    router=router,
                    state=router_state(router, current, answered, scan),
                    score=score,
                    observation=obs,
                    is_current=is_current,
                )
            )
            current_score = record.score if is_current and record else None
            if obs is not None or current_score is not None:
                busy_value = obs.busyness.value if obs else None
                self._store.add_hourly(router.id, now, busy_value, current_score)
        self._store.add_scores(now, [(st.router.id, st.score) for st in statuses if st.score])

        # Alerts for the current router.
        alert: Alert | None = None
        if current is not None and record is not None:
            alert = self._alerts.process(
                current.id, current.name, now, record.verdict, record.reasons
            )
            if alert is not None:
                self._store.add_event(
                    Event(now, current.id, alert.kind.value, f"{alert.title}: {alert.message}")
                )
                if self._notifier is not None and s.notifications_enabled:
                    self._notifier.notify(alert)
        unstable = current is not None and self._alerts.is_unstable(current.id)
        confirmed_unstable = recovering = False
        if current is not None and record is not None:
            snap = self._alerts.snapshot(current.id)
            confirmed_unstable = snap.bad_streak >= self._alerts.unstable_checks
            recovering = snap.alerting and record.verdict is Verdict.OK

        recommendation = recommend(scored, current.id if current else None, now)
        if recommendation is not None:
            for st in statuses:
                st.recommended = st.router.id == recommendation.router_id

        self._maybe_purge(now)
        return CycleReport(
            timestamp=now,
            settings=s,
            location_allowed=location_allowed,
            wifi_error=wifi_error,
            gateway=gateway,
            connection=connection,
            match=match,
            test=test,
            record=record,
            scan=scan,
            statuses=statuses,
            alert=alert,
            recommendation=recommendation,
            unstable=unstable,
            next_check_at=next_check_at(now, now, s.interval_min, unstable),
            linked=linked,
            confirmed_unstable=confirmed_unstable,
            recovering=recovering,
        )

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _current_rssi(
        connection: WifiConnection | None, scan: list[ScanEntry] | None
    ) -> int | None:
        if connection is None:
            return None
        if scan is not None and connection.bssid is not None:
            entry = next((e for e in scan if e.bssid == connection.bssid), None)
            if entry is not None:
                return entry.rssi
        return quality_to_rssi(connection.signal_quality)

    def _recent_jitter(self, router_id: str, now: datetime) -> float | None:
        jitters = [
            j
            for c in self._store.checks(router_id, now - HISTORY_SPAN)
            if (
                j := c.internet_jitter_ms
                if c.internet_jitter_ms is not None
                else c.gateway_jitter_ms
            )
            is not None
        ]
        return statistics.median(jitters) if jitters else None

    def _score_for(
        self, router: Router, obs: ScanObservation | None, now: datetime
    ) -> tuple[Score | None, datetime | None]:
        """Measured rolling score if there are recent full tests, else an estimate."""
        history = [
            c for c in self._store.checks(router.id, now - HISTORY_SPAN) if c.score is not None
        ]
        recent = [(c.timestamp, c.score) for c in history if now - c.timestamp <= WINDOW]
        measured = rolling_score(recent, now)
        if measured is not None:
            return make_score(measured, estimated=False), recent[-1][0]

        past = statistics.fmean(c.score for c in history) if history else None
        if obs is None:
            return make_score(past, estimated=True), None
        value = estimated_score(obs.entry.rssi, obs.busyness.value, past)
        return make_score(value, estimated=True), obs.timestamp

    def _maybe_purge(self, now: datetime) -> None:
        if self._last_purge is not None and now - self._last_purge < PURGE_EVERY:
            return
        self._store.purge(now - timedelta(days=self._settings.retention_days))
        self._last_purge = now
