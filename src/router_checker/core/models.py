"""Data models shared by the core, the platform services and the UI.

All timestamps are timezone-aware datetimes in UTC; convert for display.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from router_checker.core.mac import MacAddress

_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")

DEFAULT_ROUTER_COLORS = ("#0078D4", "#107C10", "#D83B01", "#8764B8", "#008575", "#C239B3")


class Band(StrEnum):
    GHZ_2_4 = "2.4 GHz"
    GHZ_5 = "5 GHz"
    GHZ_6 = "6 GHz"
    UNKNOWN = "unknown"


class LinkKind(StrEnum):
    """How the PC reaches the router."""

    WIFI = "wifi"
    ETHERNET = "ethernet"

    @property
    def label(self) -> str:
        return "Wi-Fi" if self is LinkKind.WIFI else "Ethernet"


class LinkChoice(StrEnum):
    """Which connection to check (Settings > Connection)."""

    AUTO = "auto"  # the one Windows uses for the internet
    WIFI = "wifi"
    ETHERNET = "ethernet"


class RouterState(StrEnum):
    ONLINE = "Online"  # connected and the full test got answers
    VISIBLE = "Visible"  # in range, not tested
    NOT_FOUND = "Not found"  # scan worked but the router was not in it
    UNKNOWN = "Unknown"  # no location permission, so no scan


class Verdict(StrEnum):
    OK = "OK"
    UNSTABLE = "Unstable"
    INTERNET_DOWN = "Internet provider problem"  # gateway answers, all targets fail
    ROUTER_UNREACHABLE = "Router not responding"  # gateway and targets both fail
    NOT_CONNECTED = "Not connected"


class InstabilityReason(StrEnum):
    HIGH_LOSS = "high packet loss"
    HIGH_GATEWAY_LATENCY = "high gateway latency"
    HIGH_JITTER = "high jitter"
    ALL_TARGETS_FAILED = "all internet targets failing"
    GATEWAY_UNREACHABLE = "router not responding"


class BusyLevel(StrEnum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


@dataclass(frozen=True, slots=True)
class Router:
    id: str
    name: str
    color: str
    ssid: str | None = None
    macs: tuple[MacAddress, ...] = ()
    address: str | None = None  # its own IP, seen from behind your middle router
    middle_bssid: MacAddress | None = None  # the Wi-Fi MAC the middle router last joined

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("router name must not be empty")
        if not _COLOR.match(self.color):
            raise ValueError(f"router color must look like #RRGGBB, got {self.color!r}")

    @classmethod
    def create(
        cls,
        name: str,
        *,
        color: str = DEFAULT_ROUTER_COLORS[0],
        ssid: str | None = None,
        macs: Iterable[MacAddress] = (),
    ) -> Router:
        return cls(uuid.uuid4().hex, name.strip(), color, ssid or None, _unique(macs))

    def has_mac(self, mac: MacAddress | None) -> bool:
        return mac is not None and mac in self.macs

    def with_macs(self, *new: MacAddress) -> Router:
        return replace(self, macs=_unique((*self.macs, *new)))

    def with_learned(self, other: Router) -> Router:
        """Add what the app learned about ``other`` (the same router): MACs, and its
        address and Wi-Fi MAC behind the middle router."""
        return replace(
            self.with_macs(*other.macs),
            address=other.address or self.address,
            middle_bssid=other.middle_bssid or self.middle_bssid,
        )


def _unique(macs: Iterable[MacAddress]) -> tuple[MacAddress, ...]:
    return tuple(dict.fromkeys(macs))


@dataclass(frozen=True, slots=True)
class PingStats:
    """Summary of one series of pings. RTT fields are None when nothing answered."""

    samples: tuple[float | None, ...]  # RTT in ms, None = lost
    sent: int
    received: int
    loss_pct: float
    avg_ms: float | None
    median_ms: float | None
    p95_ms: float | None
    min_ms: float | None
    max_ms: float | None
    jitter_ms: float | None  # mean |RTT[i] - RTT[i-1]| over answered pings

    @property
    def all_lost(self) -> bool:
        return self.received == 0


@dataclass(frozen=True, slots=True)
class DnsResult:
    host: str
    addresses: tuple[str, ...]
    elapsed_ms: float
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.addresses)


@dataclass(frozen=True, slots=True)
class TargetResult:
    target: str
    address: str | None  # the IP that was pinged
    ping: PingStats | None  # None when DNS failed so nothing was pinged
    dns: DnsResult | None = None  # only for domain targets

    @property
    def failed(self) -> bool:
        return self.ping is None or self.ping.all_lost


@dataclass(frozen=True, slots=True)
class BssLoad:
    """802.11 BSS Load element (IE 11)."""

    station_count: int
    channel_utilization: int  # 0-255
    available_admission_capacity: int | None = None

    @property
    def utilization_pct(self) -> float:
        return self.channel_utilization * 100.0 / 255.0


@dataclass(frozen=True, slots=True)
class ScanEntry:
    """One BSS (access point radio) seen in a Wi-Fi scan."""

    ssid: str
    bssid: MacAddress
    rssi: int  # dBm
    link_quality: int  # 0-100
    frequency_mhz: int
    channel: int | None
    band: Band
    bss_load: BssLoad | None = None


@dataclass(frozen=True, slots=True)
class WifiConnection:
    """What the Wi-Fi adapter is connected to (needs location permission)."""

    ssid: str
    bssid: MacAddress | None
    signal_quality: int  # 0-100
    profile_name: str = ""
    interface_id: str = ""  # GUID string, braces included


@dataclass(frozen=True, slots=True)
class SavedNetwork:
    """A Wi-Fi profile Windows has saved. Its password stays in Windows."""

    profile_name: str
    ssid: str | None  # the Wi-Fi name it connects to; None if it couldn't be read


@dataclass(frozen=True, slots=True)
class GatewayInfo:
    """The adapter being checked and its default gateway (works without location
    permission). Pings leave through this adapter: they're sent from ``local_ip``."""

    interface_id: str
    interface_name: str
    local_ip: str | None
    gateway_ip: str
    gateway_mac: MacAddress | None
    kind: LinkKind = LinkKind.WIFI
    vpn: bool = False  # a VPN (or another adapter) carries the internet traffic

    @property
    def wired(self) -> bool:
        return self.kind is LinkKind.ETHERNET


@dataclass(frozen=True, slots=True)
class Busyness:
    value: float  # 0 (idle) .. 1 (very busy)
    level: BusyLevel
    estimated: bool  # True when no BSS Load element was available


@dataclass(frozen=True, slots=True)
class ScanObservation:
    """A configured router as seen in one scan."""

    timestamp: datetime
    router_id: str
    entry: ScanEntry
    same_channel_count: int
    busyness: Busyness


@dataclass(frozen=True, slots=True)
class FullTestResult:
    timestamp: datetime
    router_id: str | None
    ssid: str | None
    bssid: MacAddress | None
    gateway: GatewayInfo | None
    gateway_ping: PingStats | None
    targets: tuple[TargetResult, ...]
    rssi: int | None
    signal_quality: int | None
    via_vpn: bool = False  # the internet targets only answered through the VPN


@dataclass(frozen=True, slots=True)
class CheckRecord:
    """Flattened result of one full test; this is what history stores."""

    timestamp: datetime
    router_id: str | None
    ssid: str | None
    bssid: str | None
    verdict: Verdict
    reasons: tuple[InstabilityReason, ...]
    gateway_loss_pct: float | None
    gateway_avg_ms: float | None
    gateway_p95_ms: float | None
    gateway_jitter_ms: float | None
    gateway_silent: bool  # gateway ignores ping while the internet works
    internet_loss_pct: float | None
    internet_latency_ms: float | None
    internet_jitter_ms: float | None
    dns_ms: float | None
    rssi: int | None
    signal_quality: int | None
    score: float | None = None
    link: LinkKind = LinkKind.WIFI
    via_vpn: bool = False  # internet numbers were measured through the VPN

    @property
    def unstable(self) -> bool:
        return bool(self.reasons)


@dataclass(frozen=True, slots=True)
class Score:
    value: int  # 0-100
    estimated: bool

    @property
    def label(self) -> str:
        from router_checker.core.scoring import score_label

        return score_label(self.value)


class AlertKind(StrEnum):
    UNSTABLE = "unstable"
    RECOVERED = "recovered"


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    router_id: str
    router_name: str
    timestamp: datetime
    verdict: Verdict
    reasons: tuple[InstabilityReason, ...] = ()
    recommended_name: str | None = None  # a router that scores clearly better right now
    recommended_score: Score | None = None
    recommended_id: str | None = None

    @property
    def title(self) -> str:
        if self.kind is AlertKind.RECOVERED:
            return f"{self.router_name} is back to normal"
        if self.verdict is Verdict.INTERNET_DOWN:
            return f"Internet provider problem on {self.router_name}"
        if self.verdict is Verdict.ROUTER_UNREACHABLE:
            return f"{self.router_name} is not responding"
        return f"{self.router_name} is unstable"

    @property
    def message(self) -> str:
        if self.kind is AlertKind.RECOVERED:
            return "The last checks were stable."
        if self.verdict is Verdict.INTERNET_DOWN:
            return "The router answers, but no internet target does."
        if self.verdict is Verdict.ROUTER_UNREACHABLE:
            return "Neither the router nor the internet answered."
        text = ", ".join(r.value for r in self.reasons) or self.verdict.value
        return f"{text[:1].upper()}{text[1:]}."

    @property
    def suggestion(self) -> str:
        """ "Try Cafe instead (score 85)." when a better router is known, else ""."""
        if self.kind is AlertKind.RECOVERED or self.recommended_name is None:
            return ""
        score = self.recommended_score
        if score is None:
            return f"Try {self.recommended_name} instead."
        return f"Try {self.recommended_name} instead (score {'~' * score.estimated}{score.value})."

    @property
    def text(self) -> str:
        """The message and the suggestion together."""
        return f"{self.message} {self.suggestion}".strip()


@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    router_id: str | None
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class ScorePoint:
    timestamp: datetime
    value: int
    estimated: bool


@dataclass(frozen=True, slots=True)
class HourlyAggregate:
    router_id: str
    hour_start: datetime
    busyness_avg: float | None
    score_avg: float | None


@dataclass(frozen=True, slots=True)
class Recommendation:
    router_id: str
    score: Score
    current_score: Score | None
    margin: int | None


@dataclass(slots=True)
class RouterStatus:
    router: Router
    state: RouterState
    score: Score | None = None
    observation: ScanObservation | None = None
    is_current: bool = False
    recommended: bool = False
