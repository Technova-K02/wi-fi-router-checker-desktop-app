"""Test doubles for the core protocols and small builders."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from router_checker.core.errors import LocationPermissionError, WifiUnavailableError
from router_checker.core.mac import MacAddress
from router_checker.core.models import (
    Alert,
    Band,
    BssLoad,
    CheckRecord,
    DnsResult,
    GatewayInfo,
    LinkChoice,
    LinkKind,
    Router,
    SavedNetwork,
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
    missing: bool = False  # the PC has no Wi-Fi adapter

    def _check(self) -> None:
        if self.missing:
            raise WifiUnavailableError("No Wi-Fi adapter found.")
        if self.denied:
            raise LocationPermissionError("denied")

    def current_connection(self) -> WifiConnection | None:
        self._check()
        return self.connection

    def scan(self, stop: threading.Event | None = None) -> list[ScanEntry]:
        self._check()
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
    """Returns a fixed series per address; unknown addresses time out. A set ``stop``
    ends the series at once, like the real service."""

    replies: dict[str, list[float | None]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    sources: list[str | None] = field(default_factory=list)  # the source of each call
    blocked: set[tuple[str | None, str]] = field(default_factory=set)  # (source, address)

    def ping(
        self,
        address: str,
        count: int,
        timeout_ms: int,
        spacing_ms: int,
        stop: threading.Event | None = None,
        source: str | None = None,
    ) -> list[float | None]:
        self.calls.append(address)
        self.sources.append(source)
        if stop is not None and stop.is_set():
            return []
        if (source, address) in self.blocked:
            return [None] * count
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
    """``wifi`` is the Wi-Fi adapter's gateway, ``cable`` the Ethernet one's.
    Automatic picks the cable when there is one, as Windows would; ``vpn`` says a
    VPN carries the internet traffic."""

    wifi: GatewayInfo | None = None
    cable: GatewayInfo | None = None
    vpn: bool = False
    choices: list[LinkChoice] = field(default_factory=list)

    def gateway(self, choice: LinkChoice = LinkChoice.AUTO) -> GatewayInfo | None:
        self.choices.append(choice)
        if choice is LinkChoice.WIFI:
            found = self.wifi
        elif choice is LinkChoice.ETHERNET:
            found = self.cable
        else:
            found = self.cable or self.wifi
        return replace(found, vpn=self.vpn) if found else None

    def wifi_gateway(self) -> GatewayInfo | None:
        return self.wifi


@dataclass
class FakeSwitcher:
    """Connecting changes what FakeWifi and FakeNetInfo report, as Windows would.
    Unknown or ``unreachable`` profiles leave Wi-Fi disconnected."""

    wifi: FakeWifi
    netinfo: FakeNetInfo
    networks: dict[str, tuple[WifiConnection, GatewayInfo]] = field(default_factory=dict)
    saved: list[SavedNetwork] = field(default_factory=list)
    unreachable: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    on_connect: object = None  # called with the profile name before connecting

    def saved_networks(self) -> list[SavedNetwork]:
        return list(self.saved)

    def connect(self, profile_name: str) -> None:
        self.calls.append(f"connect {profile_name}")
        if callable(self.on_connect):
            self.on_connect(profile_name)
        if profile_name in self.unreachable or profile_name not in self.networks:
            self.wifi.connection, self.netinfo.wifi = None, None
            return
        self.wifi.connection, self.netinfo.wifi = self.networks[profile_name]

    def disconnect(self) -> None:
        self.calls.append("disconnect")
        self.wifi.connection, self.netinfo.wifi = None, None


@dataclass
class FakeIdle:
    seconds: float = 0.0

    def idle_seconds(self) -> float:
        return self.seconds


@dataclass
class FakeStartup:
    """Start with Windows; ``fail`` makes Windows refuse the change."""

    on: bool = False
    fail: bool = False

    def enabled(self) -> bool:
        return self.on

    def set_enabled(self, on: bool) -> None:
        if self.fail:
            raise PermissionError(5, "Access is denied")
        self.on = on


@dataclass
class FakeNotifier:
    alerts: list[Alert] = field(default_factory=list)

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)


def gateway_info(ip: str = "192.168.1.1", gw_mac: str | None = "B0-0A-D5-9A-7B-B4") -> GatewayInfo:
    return GatewayInfo("{GUID}", "Wi-Fi", WIFI_IP, ip, mac(gw_mac) if gw_mac else None)


def cable_gateway(ip: str = "192.168.1.1", gw_mac: str | None = "B0-0A-D5-9A-7B-B4") -> GatewayInfo:
    """A cable into the ZTE (its LAN MAC), by default."""
    return GatewayInfo(
        "{CABLE}", "Ethernet", CABLE_IP, ip, mac(gw_mac) if gw_mac else None, LinkKind.ETHERNET
    )


WIFI_IP = "192.168.1.50"
CABLE_IP = "192.168.1.60"


# --- a small simulated network: the ZTE router plus two neighbours ------------------

GW = "192.168.1.1"
ZTE_LAN = "B0-0A-D5-9A-7B-B4"
ZTE_BSSID = "B0-0A-D5-9A-7B-B8"
NB_GW = "10.0.0.1"
NB_LAN = "22-22-22-22-22-20"
NB_BSSID = "22-22-22-22-22-22"
CAFE_BSSID = "33-33-33-33-33-33"
GOOD = [10.0, 12.0, 11.0, 10.0]


def sample_routers() -> tuple[Router, Router, Router]:
    zte = Router("zte", "ZTE", "#0078D4", macs=(mac(ZTE_LAN),))
    neighbor = Router("nb", "Neighbor", "#107C10", ssid="Neighbor")
    gone = Router("gone", "Gone", "#D83B01", ssid="Gone")
    return zte, neighbor, gone


def zte_connection() -> tuple[WifiConnection, GatewayInfo]:
    return WifiConnection("ZTE-Home", mac(ZTE_BSSID), 90, "ZTE-Home"), gateway_info(GW, ZTE_LAN)


def neighbor_connection() -> tuple[WifiConnection, GatewayInfo]:
    return WifiConnection("Neighbor", mac(NB_BSSID), 70, "Neighbor"), gateway_info(NB_GW, NB_LAN)


def network_parts() -> dict[str, object]:
    """Fake services for the engine: connected to the ZTE, everything answering.

    Windows has saved "ZTE-Home", "Neighbor" and "Gone"; the switcher can connect to
    the first two.
    """
    from router_checker.core.storage import SqliteHistoryStore

    zte_conn, zte_gw = zte_connection()
    wifi = FakeWifi(
        connection=zte_conn,
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
            entry(NB_BSSID, "Neighbor", rssi=-60),
            entry(CAFE_BSSID, "Cafe", rssi=-70, channel=36, freq=5180, band=Band.GHZ_5),
        ],
    )
    netinfo = FakeNetInfo(zte_gw)
    switcher = FakeSwitcher(
        wifi,
        netinfo,
        networks={"ZTE-Home": (zte_conn, zte_gw), "Neighbor": neighbor_connection()},
        saved=[SavedNetwork(n, n) for n in ("ZTE-Home", "Neighbor", "Gone")],
    )
    return {
        "wifi": wifi,
        "ping": FakePing(
            {GW: [2.0], NB_GW: [4.0], "1.1.1.1": GOOD, "8.8.8.8": GOOD, "142.250.0.1": GOOD}
        ),
        "dns": FakeDns({"google.com": "142.250.0.1"}),
        "netinfo": netinfo,
        "store": SqliteHistoryStore(":memory:"),
        "clock": FakeClock(),
        "notifier": FakeNotifier(),
        "switcher": switcher,
    }


ENGINE_PARTS = ("wifi", "ping", "dns", "netinfo", "store", "clock", "notifier")


def make_engine(settings, **overrides):
    from router_checker.core.checker import CheckEngine

    parts = network_parts()
    parts.update(overrides)
    return CheckEngine(settings, **{k: parts[k] for k in ENGINE_PARTS}), parts
