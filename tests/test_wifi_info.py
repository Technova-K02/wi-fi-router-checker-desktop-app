import pytest

from fakes import entry, mac
from router_checker.core.models import Band, BssLoad, Router
from router_checker.core.wifi_info import (
    band_from_frequency,
    best_entry_for,
    channel_from_frequency,
    iter_ies,
    parse_bss_load,
    quality_to_rssi,
    same_channel_count,
)


def ie(element_id: int, body: bytes) -> bytes:
    return bytes([element_id, len(body)]) + body


def test_iter_ies_and_truncation() -> None:
    data = ie(0, b"MyNet") + ie(3, b"\x06") + b"\x0b\x05\x01"  # last one truncated
    assert list(iter_ies(data)) == [(0, b"MyNet"), (3, b"\x06")]


def test_parse_bss_load() -> None:
    data = ie(0, b"x") + ie(11, bytes([0x2A, 0x01, 128, 0x10, 0x00])) + ie(221, b"vendor")
    load = parse_bss_load(data)
    assert load == BssLoad(
        station_count=298, channel_utilization=128, available_admission_capacity=16
    )
    assert load.utilization_pct == pytest.approx(50.2, abs=0.1)


def test_parse_bss_load_absent_or_short() -> None:
    assert parse_bss_load(ie(0, b"x")) is None
    assert parse_bss_load(ie(11, b"\x01\x00")) is None
    assert parse_bss_load(b"") is None


@pytest.mark.parametrize(
    ("freq", "band", "channel"),
    [
        (2412, Band.GHZ_2_4, 1),
        (2437, Band.GHZ_2_4, 6),
        (2472, Band.GHZ_2_4, 13),
        (2484, Band.GHZ_2_4, 14),
        (5180, Band.GHZ_5, 36),
        (5220, Band.GHZ_5, 44),
        (5500, Band.GHZ_5, 100),
        (5825, Band.GHZ_5, 165),
        (5955, Band.GHZ_6, 1),
        (5935, Band.GHZ_6, 2),
        (6415, Band.GHZ_6, 93),
        (900, Band.UNKNOWN, None),
    ],
)
def test_band_and_channel(freq: int, band: Band, channel: int | None) -> None:
    assert band_from_frequency(freq) is band
    assert channel_from_frequency(freq) == channel


def test_best_entry_priority_bssid_then_ssid_then_same_device() -> None:
    r = Router("r", "R", "#000000", ssid="Home", macs=(mac("AA-AA-AA-AA-AA-01"),))
    known = entry("AA-AA-AA-AA-AA-01", ssid="Other", rssi=-80)
    by_ssid = entry("11-11-11-11-11-11", ssid="Home", rssi=-40)
    near = entry("AA-AA-AA-AA-AA-05", ssid="Guest", rssi=-30)
    assert best_entry_for(r, [by_ssid, known, near]) == known
    assert best_entry_for(r, [by_ssid, near]) == by_ssid
    assert best_entry_for(r, [near]) == near
    assert best_entry_for(r, [entry("22-22-22-22-22-22")]) is None


def test_best_entry_picks_strongest() -> None:
    r = Router("r", "R", "#000000", ssid="Home")
    weak = entry("11-11-11-11-11-11", ssid="Home", rssi=-70)
    strong = entry("11-11-11-11-11-12", ssid="Home", rssi=-45)
    assert best_entry_for(r, [weak, strong]) == strong


def test_same_channel_count_excludes_own_radios_and_other_bands() -> None:
    mine = entry("AA-AA-AA-AA-AA-01", channel=6)
    mine_guest = entry("AA-AA-AA-AA-AA-02", channel=6)
    others = [
        entry("11-11-11-11-11-11", channel=6),
        entry("22-22-22-22-22-22", channel=6),
        entry("33-33-33-33-33-33", channel=11),
        entry("44-44-44-44-44-44", channel=6, band=Band.GHZ_5, freq=5030),
    ]
    all_entries = [mine, mine_guest, *others]
    assert same_channel_count(mine, all_entries, [mine_guest.bssid]) == 2
    assert same_channel_count(mine, all_entries) == 3


def test_quality_to_rssi() -> None:
    assert quality_to_rssi(100) == -50
    assert quality_to_rssi(0) == -100
    assert quality_to_rssi(60) == -70
    assert quality_to_rssi(150) == -50
