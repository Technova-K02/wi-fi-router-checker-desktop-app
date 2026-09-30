"""Interfaces the core depends on. Windows implementations live in
``router_checker.platform_windows``; tests use fakes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from router_checker.core.models import (
    Alert,
    CheckRecord,
    DnsResult,
    Event,
    GatewayInfo,
    HourlyAggregate,
    Recommendation,
    Router,
    ScanEntry,
    ScanObservation,
    Score,
    ScorePoint,
    WifiConnection,
)


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware (UTC)."""
        ...


class WifiService(Protocol):
    """Native Wifi API. Methods raise LocationPermissionError or WifiUnavailableError."""

    def current_connection(self) -> WifiConnection | None: ...

    def scan(self) -> list[ScanEntry]:
        """Trigger a scan, wait briefly for it, and return the BSS list."""
        ...

    def saved_profiles(self) -> list[str]: ...

    def location_allowed(self) -> bool:
        """Quick permission probe without a scan (raises WifiUnavailableError)."""
        ...


class PingService(Protocol):
    def ping(
        self, address: str, count: int, timeout_ms: int, spacing_ms: int
    ) -> list[float | None]:
        """RTT in ms per echo request, None for each lost one. ``address`` is an IP."""
        ...


class DnsService(Protocol):
    def resolve(self, host: str) -> DnsResult: ...


class NetworkInfoService(Protocol):
    def wifi_gateway(self) -> GatewayInfo | None:
        """Default gateway of the connected Wi-Fi adapter, or None if not connected."""
        ...


class NetworkChangeWatcher(Protocol):
    """Tells when Windows changed network routes (e.g. switched Wi-Fi).

    Callbacks arrive on system threads, so implementations only record the
    change; the app polls ``take_change`` from its own thread.
    """

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def take_change(self) -> bool:
        """True once for each burst of changes since the previous call."""
        ...


class NotificationService(Protocol):
    def notify(self, alert: Alert) -> None: ...


class HistoryStore(Protocol):
    def add_check(self, record: CheckRecord) -> None: ...

    def checks(self, router_id: str, since: datetime) -> list[CheckRecord]: ...

    def recent_checks(self, limit: int, router_id: str | None = None) -> list[CheckRecord]: ...

    def add_scores(self, when: datetime, scores: Sequence[tuple[str, Score]]) -> None: ...

    def scores(self, router_id: str, since: datetime) -> list[ScorePoint]: ...

    def add_observations(self, observations: Sequence[ScanObservation]) -> None: ...

    def latest_observation(self, router_id: str) -> ScanObservation | None: ...

    def add_hourly(
        self, router_id: str, when: datetime, busyness: float | None, score: float | None
    ) -> None: ...

    def hourly(self, router_id: str, since: datetime) -> list[HourlyAggregate]: ...

    def add_event(self, event: Event) -> None: ...

    def events(self, router_id: str | None, limit: int) -> list[Event]: ...

    def purge(self, before: datetime) -> None: ...


class RouterSwitcher(Protocol):
    """Connects to another router with a profile Windows already saved (Phase 4/5)."""

    def saved_profiles(self) -> list[str]: ...

    def connect(self, profile_name: str) -> None:
        """Start connecting; returns without waiting for an IP address."""
        ...


class SwitchingPolicy(Protocol):
    """Decides whether to switch automatically (Phase 5).

    Planned rules: the candidate must be better for 3 checks in a row, a
    30-minute cooldown between switches, and switching back if the new
    router fails.
    """

    def decide(
        self,
        now: datetime,
        current: Router | None,
        current_score: Score | None,
        recommendation: Recommendation | None,
    ) -> Router | None:
        """The router to switch to, or None to stay."""
        ...
