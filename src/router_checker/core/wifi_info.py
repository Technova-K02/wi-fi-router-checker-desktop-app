"""Decoding Wi-Fi scan data: information elements, channels, bands, crowding."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from xml.etree import ElementTree

from router_checker.core.mac import MacAddress
from router_checker.core.models import Band, BssLoad, Router, ScanEntry

IE_SSID = 0
IE_BSS_LOAD = 11
_PROFILE_NS = "{http://www.microsoft.com/networking/WLAN/profile/v1}"


def iter_ies(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Yield (element id, body) pairs; stops quietly at a truncated element."""
    pos = 0
    while pos + 2 <= len(data):
        element_id, length = data[pos], data[pos + 1]
        body = data[pos + 2 : pos + 2 + length]
        if len(body) < length:
            return
        yield element_id, body
        pos += 2 + length


def parse_bss_load(ies: bytes) -> BssLoad | None:
    """Decode the BSS Load element (IE 11) if the AP broadcasts it.

    Layout (IEEE 802.11-2020 9.4.2.27): station count (uint16 LE),
    channel utilization (uint8, 0-255), available admission capacity (uint16 LE).
    """
    for element_id, body in iter_ies(ies):
        if element_id == IE_BSS_LOAD and len(body) >= 3:
            capacity = int.from_bytes(body[3:5], "little") if len(body) >= 5 else None
            return BssLoad(
                station_count=int.from_bytes(body[0:2], "little"),
                channel_utilization=body[2],
                available_admission_capacity=capacity,
            )
    return None


def band_from_frequency(freq_mhz: int) -> Band:
    if 2400 <= freq_mhz < 2500:
        return Band.GHZ_2_4
    if 5150 <= freq_mhz < 5925:
        return Band.GHZ_5
    if 5925 <= freq_mhz <= 7125:
        return Band.GHZ_6
    return Band.UNKNOWN


def channel_from_frequency(freq_mhz: int) -> int | None:
    band = band_from_frequency(freq_mhz)
    if band is Band.GHZ_2_4:
        if freq_mhz == 2484:
            return 14
        return (freq_mhz - 2407) // 5
    if band is Band.GHZ_5:
        return (freq_mhz - 5000) // 5
    if band is Band.GHZ_6:
        if freq_mhz == 5935:
            return 2
        return (freq_mhz - 5950) // 5
    return None


def entry_matches_router(entry: ScanEntry, router: Router) -> bool:
    return entry.bssid in router.macs or (router.ssid is not None and entry.ssid == router.ssid)


def entry_same_device(entry: ScanEntry, router: Router) -> bool:
    """The BSSID differs from one of the router's MACs only in the last byte."""
    return any(entry.bssid.same_device(mac) for mac in router.macs)


def best_entry_for(router: Router, entries: Sequence[ScanEntry]) -> ScanEntry | None:
    """Strongest BSS of this router.

    Priority: a known BSSID, then the router's SSID, then a BSSID that differs
    from one of its MACs only in the last byte (for routers known only by their
    LAN MAC). The last one is a heuristic.
    """
    for matches in (
        lambda e: e.bssid in router.macs,
        lambda e: router.ssid is not None and e.ssid == router.ssid,
        lambda e: entry_same_device(e, router),
    ):
        candidates = [e for e in entries if matches(e)]
        if candidates:
            return max(candidates, key=lambda e: e.rssi)
    return None


def same_channel_count(
    entry: ScanEntry, entries: Sequence[ScanEntry], own: Sequence[MacAddress] = ()
) -> int:
    """Other BSSs on the same band and channel (the router's own radios excluded)."""
    if entry.channel is None:
        return 0
    excluded = {entry.bssid, *own}
    return sum(
        1
        for e in entries
        if e.channel == entry.channel and e.band is entry.band and e.bssid not in excluded
    )


def ssid_from_profile_xml(xml: str) -> str | None:
    """The Wi-Fi name a saved profile connects to (``SSIDConfig/SSID``), or None.

    Only the name is read; the rest of the profile, including its key, is ignored.
    ``hex`` (the raw bytes, decoded like scan results) wins over ``name``.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return None
    ssid = root.find(f"{_PROFILE_NS}SSIDConfig/{_PROFILE_NS}SSID")
    if ssid is None:
        return None
    raw = ssid.findtext(f"{_PROFILE_NS}hex")
    if raw:
        try:
            return bytes.fromhex(raw.strip()).decode("utf-8", errors="replace")
        except ValueError:
            pass
    return ssid.findtext(f"{_PROFILE_NS}name") or None


def quality_to_rssi(quality: int) -> int:
    """Windows maps signal quality 0..100 linearly to -100..-50 dBm."""
    quality = max(0, min(100, quality))
    return round(quality / 2 - 100)
