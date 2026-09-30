import pytest

from fakes import T0, gateway_info, mac
from router_checker.core.alerts import Thresholds
from router_checker.core.measurements import summarize_internet, to_record
from router_checker.core.models import (
    DnsResult,
    FullTestResult,
    InstabilityReason,
    TargetResult,
    Verdict,
)
from router_checker.core.stats import summarize


def t(samples, name="1.1.1.1", dns=None):
    return TargetResult(name, name, None if samples is None else summarize(samples), dns)


def test_summarize_internet() -> None:
    targets = (
        t([10.0, 20.0, 10.0, 20.0]),  # median 15, jitter 10
        t([30.0, None, 30.0, 30.0]),  # median 30, jitter 0, 1 lost
        t(None, "bad.example", DnsResult("bad.example", (), 3.0, "nx")),  # DNS failed: 4 lost
    )
    s = summarize_internet(targets, pings_per_target=4)
    assert s.loss_pct == pytest.approx(100 * 5 / 12)
    assert s.latency_ms == pytest.approx(22.5)
    assert s.jitter_ms == pytest.approx(5.0)
    assert s.dns_ms is None


def test_summarize_internet_no_targets() -> None:
    s = summarize_internet((), 10)
    assert s.loss_pct is None and s.latency_ms is None


def test_to_record() -> None:
    result = FullTestResult(
        timestamp=T0,
        router_id="r1",
        ssid="Home",
        bssid=mac("aa:bb:cc:dd:ee:ff"),
        gateway=gateway_info(),
        gateway_ping=summarize([1.0, 2.0, 1.0]),
        targets=(
            t([None, None, None]),
            t([None, None, None], "google.com", DnsResult("google.com", ("1.2.3.4",), 25.0)),
        ),
        rssi=-55,
        signal_quality=90,
    )
    rec = to_record(result, Thresholds(), pings_per_target=3)
    assert rec.verdict is Verdict.INTERNET_DOWN
    assert rec.reasons == (InstabilityReason.ALL_TARGETS_FAILED,)
    assert rec.bssid == "AA-BB-CC-DD-EE-FF"
    assert rec.internet_loss_pct == 100.0
    assert rec.dns_ms == 25.0
    assert rec.gateway_avg_ms == pytest.approx(4 / 3)
    assert rec.unstable
