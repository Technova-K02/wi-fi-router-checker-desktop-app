"""A middle router: your own router between the PC (on a cable) and your routers.

    PC ──cable──▶ middle router ──Wi-Fi──▶ one of your routers ──▶ internet

The PC's gateway is then the middle router, so the router in use is found one
hop further: a ping toward an internet target that may only travel 2 hops
comes back "time exceeded" from that router, which tells its address. Each
router's address is learned when the app switches to it (or typed in).

Switching asks the middle router over plain HTTP, without a login:
``http://<host>:<port>/change_router?router=<Wi-Fi MAC>``. Nothing else is sent
to it, and no other router is ever contacted this way.

A middle router that isn't set up yet is spotted without any HTTP: on a cable,
the gateway isn't one of your routers and the second hop is another private
address (a router behind a router). That's only a suggestion; nothing is sent
to it until you confirm it with its port.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from dataclasses import dataclass

from router_checker.core.mac import MacAddress
from router_checker.core.models import GatewayInfo, Router, ScanEntry

DEFAULT_PORT = 80
UPSTREAM_HOPS = 2  # PC -> middle router (1) -> the router in use (2)
MAX_BSSID_TRIES = 3  # Wi-Fi MACs tried per switch when the first doesn't work

_PRIVATE = tuple(
    ipaddress.IPv4Network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_HOST = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int = DEFAULT_PORT

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"

    def change_url(self, bssid: MacAddress) -> str:
        return f"http://{self}/change_router?router={colon_mac(bssid)}"

    def is_gateway(self, gateway: GatewayInfo | None) -> bool:
        """The PC reaches the internet through this middle router."""
        return gateway is not None and gateway.gateway_ip == self.host


def parse_endpoint(text: str) -> Endpoint | None:
    """``192.168.8.1:8080``, ``192.168.8.1`` (port 80) or ``http://192.168.8.1:8080/``;
    None if it isn't one."""
    text = text.strip()
    text = re.sub(r"^https?://", "", text, flags=re.IGNORECASE).rstrip("/")
    if not text or "/" in text:
        return None
    host, _, port_text = text.partition(":")
    if port_text:
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            return None
        port = int(port_text)
    else:
        port = DEFAULT_PORT
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        if not _HOST.match(host) or host.replace(".", "").isdigit():
            return None
    return Endpoint(host, port)


def colon_mac(mac: MacAddress) -> str:
    """``b0:0a:d5:9a:7b:b8``, the form the middle router's URL takes."""
    return str(mac).replace("-", ":").lower()


def bssid_order(
    router: Router, scan: Sequence[ScanEntry] | None, observed: MacAddress | None = None
) -> tuple[MacAddress, ...]:
    """The router's Wi-Fi MACs to ask the middle router for, best first: the one that
    worked last time, the ones in the last scan (strongest first), the one last
    observed, then the rest in the order they're listed. At most MAX_BSSID_TRIES."""
    seen = sorted(
        (e for e in scan or () if e.bssid in router.macs), key=lambda e: e.rssi, reverse=True
    )
    order = [router.middle_bssid, *(e.bssid for e in seen), observed, *router.macs]
    wanted = [m for m in order if m is not None and (m in router.macs or m == router.middle_bssid)]
    return tuple(dict.fromkeys(wanted))[:MAX_BSSID_TRIES]


def is_private_ipv4(address: str | None) -> bool:
    """A home-network address (10.x, 172.16-31.x, 192.168.x); not the internet, and not
    a provider's shared 100.64.x range."""
    try:
        ip = ipaddress.IPv4Address(address or "")
    except ValueError:
        return False
    return any(ip in net for net in _PRIVATE)


def gateway_key(gateway: GatewayInfo) -> str:
    """What a dismissed suggestion remembers: the gateway's MAC, else its address."""
    return str(gateway.gateway_mac) if gateway.gateway_mac else gateway.gateway_ip


def could_be_middle(
    gateway: GatewayInfo | None,
    routers: Sequence[Router],
    middle_set: bool,
    dismissed: Sequence[str] = (),
) -> bool:
    """Worth looking one hop further for a middle router: on a cable, none set up, a
    private gateway that isn't one of your routers, and not dismissed before."""
    if gateway is None or middle_set or not gateway.wired:
        return False
    if not is_private_ipv4(gateway.gateway_ip) or gateway_key(gateway) in dismissed:
        return False
    mac = gateway.gateway_mac
    return mac is None or not any(mac in r.macs for r in routers)


def middle_suggestion(gateway: GatewayInfo, upstream_ip: str | None) -> str | None:
    """The gateway's address when it looks like a middle router: the second hop is
    another private address, so a router sits behind it. None otherwise."""
    if is_private_ipv4(upstream_ip) and upstream_ip != gateway.gateway_ip:
        return gateway.gateway_ip
    return None


def router_at(routers: Sequence[Router], address: str | None) -> Router | None:
    """The router with this address behind the middle router; None when none has it,
    or when several do (then the address can't tell them apart)."""
    found = [r for r in routers if address is not None and r.address == address]
    return found[0] if len(found) == 1 else None
