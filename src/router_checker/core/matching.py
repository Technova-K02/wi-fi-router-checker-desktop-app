"""Working out which configured router the PC is connected to.

Order: connected BSSID, then SSID (only if exactly one router has it), then
the gateway's LAN MAC from the ARP table. The last one still works without
location permission.

When the router was matched by BSSID or SSID, the gateway MAC and the BSSID
are linked to it automatically. When it was matched by gateway MAC, the
connected BSSID is linked. A MAC another router already owns is never linked.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from router_checker.core.mac import MacAddress
from router_checker.core.models import GatewayInfo, Router, RouterState, ScanEntry, WifiConnection
from router_checker.core.wifi_info import best_entry_for


class MatchMethod(StrEnum):
    BSSID = "BSSID"
    SSID = "SSID"
    GATEWAY_MAC = "gateway MAC"


@dataclass(frozen=True, slots=True)
class Match:
    router: Router | None
    method: MatchMethod | None
    macs_to_link: tuple[MacAddress, ...] = ()


def identify_current(
    routers: Sequence[Router],
    connection: WifiConnection | None,
    gateway: GatewayInfo | None,
) -> Match:
    gateway_mac = gateway.gateway_mac if gateway else None
    router: Router | None = None
    method: MatchMethod | None = None

    if connection is not None:
        if connection.bssid is not None:
            router = next((r for r in routers if connection.bssid in r.macs), None)
            method = MatchMethod.BSSID if router else None
        if router is None and connection.ssid:
            same_ssid = [r for r in routers if r.ssid == connection.ssid]
            if len(same_ssid) == 1:
                router, method = same_ssid[0], MatchMethod.SSID
            elif gateway_mac is not None:
                router = next((r for r in same_ssid if gateway_mac in r.macs), None)
                method = MatchMethod.GATEWAY_MAC if router else None

    if router is None and gateway_mac is not None:
        router = next((r for r in routers if gateway_mac in r.macs), None)
        method = MatchMethod.GATEWAY_MAC if router else None

    if router is None:
        return Match(None, None)

    to_link: list[MacAddress] = []
    candidates = [connection.bssid if connection else None]
    if method in (MatchMethod.BSSID, MatchMethod.SSID):
        candidates.insert(0, gateway_mac)
    for mac in candidates:
        if mac is None or mac.is_zero or mac in router.macs or mac in to_link:
            continue
        if any(mac in other.macs for other in routers if other.id != router.id):
            continue
        to_link.append(mac)
    return Match(router, method, tuple(to_link))


def router_state(
    router: Router,
    current: Router | None,
    current_answered: bool,
    scan: Sequence[ScanEntry] | None,
) -> RouterState:
    """State of one router. ``scan`` is None when location permission is missing."""
    if current is not None and router.id == current.id and current_answered:
        return RouterState.ONLINE
    if current is not None and router.id == current.id:
        return RouterState.VISIBLE
    if scan is None:
        return RouterState.UNKNOWN
    return RouterState.VISIBLE if best_entry_for(router, scan) else RouterState.NOT_FOUND
