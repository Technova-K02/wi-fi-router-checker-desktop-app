"""Which network adapter to check: Wi-Fi or Ethernet, never a virtual one.

Windows lists many adapters that look like Ethernet: Hyper-V and WSL switches,
VMware and VirtualBox networks, VPN adapters. Only a *physical* Wi-Fi or
Ethernet adapter that is up and has an IPv4 address and a default gateway is
a connection to a router.

Automatic (the default) takes the adapter Windows uses for the internet. When
that isn't one of them, a VPN (or another virtual adapter) carries the
internet traffic: the router is still checked through the physical adapter
underneath, Ethernet first, as Windows itself prefers a cable.
If there's no physical adapter with a gateway at all, but the internet goes
through a Wi-Fi or Ethernet type adapter that has one (a Hyper-V external
switch puts the PC's address on such a virtual adapter), that one is used.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from router_checker.core.models import LinkChoice, LinkKind


@dataclass(frozen=True, slots=True)
class Adapter:
    """One network adapter as Windows reports it."""

    interface_id: str  # GUID with braces, uppercase
    name: str
    index: int
    kind: LinkKind | None  # None: not Wi-Fi or Ethernet (tunnel, PPP, loopback…)
    physical: bool  # a real network card, not a virtual one
    up: bool
    local_ip: str | None
    gateway_ip: str | None

    @property
    def usable(self) -> bool:
        return self.up and self.kind is not None and bool(self.local_ip and self.gateway_ip)


@dataclass(frozen=True, slots=True)
class LinkChoiceResult:
    adapter: Adapter | None
    vpn: bool  # the internet goes through a VPN or another adapter


NONE = LinkChoiceResult(None, False)


def choose_link(
    adapters: Sequence[Adapter], internet_index: int | None, choice: LinkChoice
) -> LinkChoiceResult:
    """The adapter to check. ``internet_index`` is the adapter Windows routes
    internet traffic through, or None if there's no route."""
    internet = next((a for a in adapters if a.index == internet_index), None)
    candidates = [a for a in adapters if a.usable and a.physical]
    if not candidates and internet is not None and internet.usable:
        candidates = [internet]  # e.g. a Hyper-V external switch
    if choice is LinkChoice.WIFI:
        candidates = [a for a in candidates if a.kind is LinkKind.WIFI]
    elif choice is LinkChoice.ETHERNET:
        candidates = [a for a in candidates if a.kind is LinkKind.ETHERNET]
    if not candidates:
        return NONE
    if internet in candidates:
        return LinkChoiceResult(internet, False)
    # The internet goes elsewhere: through a VPN, or (with a fixed choice) through
    # the other physical adapter, which isn't a VPN.
    vpn = internet is not None and not (internet.physical and internet.usable)
    ethernet_first = sorted(candidates, key=lambda a: a.kind is not LinkKind.ETHERNET)
    return LinkChoiceResult(ethernet_first[0], vpn)
