import csv
import io
from datetime import timedelta, timezone

from fakes import T0, record
from router_checker.core.export import HEADER, check_row, safe_text, write_checks_csv
from router_checker.core.models import InstabilityReason, LinkKind, Verdict

PLUS_2 = timezone(timedelta(hours=2))
NAMES = {"r1": "Home"}


def test_row_uses_local_time_and_plain_numbers() -> None:
    row = check_row(record(score=87.4, dns_ms=None), NAMES, PLUS_2)  # T0 is 12:00 UTC
    cells = dict(zip(HEADER, row, strict=True))
    assert cells["Time"] == "2026-09-29 14:00:00"
    assert cells["UTC offset"] == "+02:00"
    assert cells["Router"] == "Home" and cells["Wi-Fi name"] == "Net"
    assert cells["Connection"] == "Wi-Fi" and cells["Internet through VPN"] == "no"
    assert cells["Result"] == "OK" and cells["Reasons"] == ""
    assert cells["Gateway average (ms)"] == "2.0"
    assert cells["Gateway answers ping"] == "yes"
    assert cells["Internet median (ms)"] == "20.0"
    assert cells["DNS lookup (ms)"] == ""  # not measured
    assert cells["Signal (dBm)"] == "-50"  # a number, not escaped
    assert cells["Score"] == "87"


def test_unstable_row_and_unknown_network() -> None:
    unstable = record(
        router_id=None,
        ssid="Cafe",
        verdict=Verdict.UNSTABLE,
        reasons=(InstabilityReason.HIGH_LOSS, InstabilityReason.HIGH_JITTER),
        gateway_silent=True,
    )
    cells = dict(zip(HEADER, check_row(unstable, NAMES, PLUS_2), strict=True))
    assert cells["Router"] == "" and cells["Wi-Fi name"] == "Cafe"
    assert cells["Result"] == "Unstable"
    assert cells["Reasons"] == "high packet loss; high jitter"
    assert cells["Gateway answers ping"] == "no"


def test_text_that_looks_like_a_formula_is_escaped() -> None:
    assert safe_text("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    for start in "+-@\t\r":
        assert safe_text(f"{start}x") == f"'{start}x"
    assert safe_text("Home") == "Home" and safe_text(None) == ""
    row = check_row(record(ssid="=cmd|' /C calc'!A0"), {"r1": "@Home"}, PLUS_2)
    cells = dict(zip(HEADER, row, strict=True))
    assert cells["Wi-Fi name"] == "'=cmd|' /C calc'!A0"
    assert cells["Router"] == "'@Home"


def test_write_csv() -> None:
    out = io.StringIO()
    records = [record(), record(timestamp=T0 + timedelta(minutes=5), ssid='Say "hi", ok')]
    assert write_checks_csv(out, records, NAMES, PLUS_2) == 2
    text = out.getvalue()
    assert text.startswith("Time,UTC offset,Router,Connection,Wi-Fi name,")
    assert text.count("\r\n") == 3  # header and two rows, Windows line ends
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(HEADER)
    assert rows[2][HEADER.index("Wi-Fi name")] == 'Say "hi", ok'  # quoting survives


def test_a_wired_check_says_ethernet() -> None:
    wired = record(link=LinkKind.ETHERNET, ssid=None, rssi=None, signal_quality=None, via_vpn=True)
    cells = dict(zip(HEADER, check_row(wired, NAMES, PLUS_2), strict=True))
    assert cells["Connection"] == "Ethernet" and cells["Wi-Fi name"] == ""
    assert cells["Signal (dBm)"] == "" and cells["Internet through VPN"] == "yes"
