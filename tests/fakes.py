"""Test doubles for the core protocols and small builders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from router_checker.core.errors import LocationPermissionError
from router_checker.core.mac import MacAddress
from router_checker.core.models import (
    Alert,
    Band,
    BssLoad,
    CheckRecord,
    DnsResult,
    GatewayInfo,
    ScanEntry,
    Verdict,
    WifiConnection,
)

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def mac(text: str) -> MacAddress:
    return MacAddress.parse(text)


def entry(
    bssid: str,
    ssid: str = "Net",
    rssi: int = -60,
    freq: int = 2412,
    channel: int | None = 1,
    band: Band = Band.GHZ_2_4,
    load: BssLoad | None = None,
) -> ScanEntry:
    return ScanEntry(ssid, mac(bssid), rssi, 80, freq, channel, band, load)


def record(**overrides: object) -> CheckRecord:
    base: dict[str, object] = {
        "timestamp": T0,
        "router_id": "r1",
        "ssid": "Net",
        "bssid": None,
        "verdict": Verdict.OK,
        "reasons": (),
        "gateway_loss_pct": 0.0,
        "gateway_avg_ms": 2.0,
        "gateway_p95_ms": 3.0,
        "gateway_jitter_ms": 0.5,
        "gateway_silent": False,
        "internet_loss_pct": 0.0,
        "internet_latency_ms": 20.0,
        "internet_jitter_ms": 0.0,
        "dns_ms": 10.0,
        "rssi": -50,
        "signal_quality": 100,
    }
    base.update(overrides)
    return CheckRecord(**base)  # type: ignore[arg-type]


@dataclass
class FakeClock:
    current: datetime = T0

    def now(self) -> datetime:
        return self.current

    def advance(self, minutes: float) -> None:
        self.current += timedelta(minutes=minutes)


@dataclass
class FakeWifi:
    connection: WifiConnection | None = None
    entries: list[ScanEntry] = field(default_factory=list)
    denied: bool = False
    profiles: list[str] = field(default_factory=list)

    def current_connection(self) -> WifiConnection | None:
        if self.denied:
            raise LocationPermissionError("denied")
        return self.connection

    def scan(self) -> list[ScanEntry]:
        if self.denied:
            raise LocationPermissionError("denied")
        return list(self.entries)

    def saved_profiles(self) -> list[str]:
        return list(self.profiles)


@dataclass
class FakePing:
    """Returns a fixed series per address; unknown addresses time out."""

    replies: dict[str, list[float | None]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def ping(
        self, address: str, count: int, timeout_ms: int, spacing_ms: int
    ) -> list[float | None]:
        self.calls.append(address)
        series = self.replies.get(address, [None])
        return [series[i % len(series)] for i in range(count)]


@dataclass
class FakeDns:
    table: dict[str, str] = field(default_factory=dict)

    def resolve(self, host: str) -> DnsResult:
        if host in self.table:
            return DnsResult(host, (self.table[host],), 12.0)
        return DnsResult(host, (), 5.0, error="not found")


@dataclass
class FakeNetInfo:
    gateway: GatewayInfo | None = None

    def wifi_gateway(self) -> GatewayInfo | None:
        return self.gateway


@dataclass
class FakeNotifier:
    alerts: list[Alert] = field(default_factory=list)

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)


def gateway_info(ip: str = "192.168.1.1", gw_mac: str | None = "B0-0A-D5-9A-7B-B4") -> GatewayInfo:
    return GatewayInfo("{GUID}", "Wi-Fi", "192.168.1.50", ip, mac(gw_mac) if gw_mac else None)
