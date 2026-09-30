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
    Router,
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

    def location_allowed(self) -> bool:
        return not self.denied


@dataclass
class FakeWatcher:
    started: bool = False
    changed: bool = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def take_change(self) -> bool:
        changed, self.changed = self.changed, False
        return changed


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


# --- a small simulated network: the ZTE router plus two neighbours ------------------

GW = "192.168.1.1"
ZTE_LAN = "B0-0A-D5-9A-7B-B4"
ZTE_BSSID = "B0-0A-D5-9A-7B-B8"
GOOD = [10.0, 12.0, 11.0, 10.0]


def sample_routers() -> tuple[Router, Router, Router]:
    zte = Router("zte", "ZTE", "#0078D4", macs=(mac(ZTE_LAN),))
    neighbor = Router("nb", "Neighbor", "#107C10", ssid="Neighbor")
    gone = Router("gone", "Gone", "#D83B01", ssid="Gone")
    return zte, neighbor, gone


def network_parts() -> dict[str, object]:
    """Fake services for the engine: connected to the ZTE, everything answering."""
    from router_checker.core.storage import SqliteHistoryStore

    return {
        "wifi": FakeWifi(
            connection=WifiConnection("ZTE-Home", mac(ZTE_BSSID), 90),
            entries=[
                entry(
                    ZTE_BSSID,
                    "ZTE-Home",
                    rssi=-50,
                    freq=5180,
                    channel=36,
                    band=Band.GHZ_5,
                    load=BssLoad(2, 20),
                ),
                entry("22-22-22-22-22-22", "Neighbor", rssi=-60),
                entry(
                    "33-33-33-33-33-33", "Cafe", rssi=-70, channel=36, freq=5180, band=Band.GHZ_5
                ),
            ],
        ),
        "ping": FakePing({GW: [2.0], "1.1.1.1": GOOD, "8.8.8.8": GOOD, "142.250.0.1": GOOD}),
        "dns": FakeDns({"google.com": "142.250.0.1"}),
        "netinfo": FakeNetInfo(gateway_info(GW, ZTE_LAN)),
        "store": SqliteHistoryStore(":memory:"),
        "clock": FakeClock(),
        "notifier": FakeNotifier(),
    }


def make_engine(settings, **overrides):
    from router_checker.core.checker import CheckEngine

    parts = network_parts()
    parts.update(overrides)
    return CheckEngine(settings, **parts), parts
