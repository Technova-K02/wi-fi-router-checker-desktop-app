from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from fakes import (
    GW,
    T0,
    FakeNetInfo,
    FakeWifi,
    entry,
    gateway_info,
    make_engine,
    sample_routers,
)
from router_checker.core.models import (
    Band,
    BssLoad,
    BusyLevel,
    Busyness,
    InstabilityReason,
    Recommendation,
    Router,
    RouterState,
    Score,
    Verdict,
    WifiConnection,
)
from router_checker.core.presentation import (
    DASH,
    TOOLTIP_MAX,
    StatusLevel,
    busy_text,
    current_metrics,
    fmt_age,
    fmt_clock,
    fmt_countdown,
    fmt_dbm,
    fmt_ms,
    fmt_pct,
    group_networks,
    overall_status,
    reasons_text,
    recommendation_text,
    score_text,
    state_detail,
    targets_text,
    tray_tooltip,
    verdict_level,
)
from router_checker.core.settings import Settings

L = StatusLevel


def run(settings=None, cycles=1, **overrides):
    engine, parts = make_engine(
        settings or Settings(pings_per_target=4, routers=sample_routers()), **overrides
    )
    report = None
    for _ in range(cycles):
        report = engine.run_cycle()
        parts["clock"].advance(1)
    return report, parts


def test_stable() -> None:
    report, _ = run()
    status = overall_status(report)
    assert status.level is L.GOOD and status.title == "Stable"


def test_waiting_failed_not_connected_unknown_network() -> None:
    assert overall_status(None).level is L.UNKNOWN
    failed = overall_status(None, failure="boom")
    assert failed.level is L.UNKNOWN and failed.title == "Check failed" and failed.detail == "boom"
    report, _ = run(netinfo=FakeNetInfo(None))
    assert overall_status(report).title == "Not connected"
    report, _ = run(
        wifi=FakeWifi(connection=WifiConnection("Cafe", None, 80)),
        netinfo=FakeNetInfo(gateway_info(GW, "99-99-99-99-99-99")),
    )
    status = overall_status(report)
    assert status.level is L.UNKNOWN and status.title == "Unknown network"
    assert '"Cafe"' in status.detail


def test_first_unstable_check_is_yellow_then_red() -> None:
    engine, parts = make_engine(Settings(pings_per_target=4, routers=sample_routers()))
    parts["ping"].replies[GW] = [2.0, None]
    first = overall_status(engine.run_cycle())
    assert first.level is L.WARNING and first.title == "Possibly unstable"
    assert first.detail == "High packet loss"
    parts["clock"].advance(1)
    second = overall_status(engine.run_cycle())
    assert second.level is L.BAD and second.title == "Unstable"
    parts["ping"].replies[GW] = [2.0]
    parts["clock"].advance(1)
    assert overall_status(engine.run_cycle()).title == "Recovering"


def test_router_and_internet_down_are_red_immediately() -> None:
    engine, parts = make_engine(Settings(pings_per_target=4, routers=sample_routers()))
    parts["ping"].replies = {GW: [2.0]}
    status = overall_status(engine.run_cycle())
    assert status.level is L.BAD and status.title == "Internet provider problem"
    parts["ping"].replies = {}
    status = overall_status(engine.run_cycle())
    assert status.level is L.BAD and status.title == "Router not responding"


def test_low_score_is_yellow() -> None:
    report, _ = run()
    current = report.current
    report.statuses[report.statuses.index(current)] = replace(current, score=Score(52, False))
    status = overall_status(report)
    assert status.level is L.WARNING and status.title == "Fair"


def test_glyphs_differ_so_status_is_not_color_only() -> None:
    assert len({level.glyph for level in L}) == 4


def test_verdict_level() -> None:
    assert verdict_level(Verdict.OK) is L.GOOD
    assert verdict_level(Verdict.UNSTABLE) is L.WARNING
    assert verdict_level(Verdict.INTERNET_DOWN) is L.BAD
    assert verdict_level(Verdict.NOT_CONNECTED) is L.UNKNOWN


@pytest.mark.parametrize(
    ("value", "text"), [(None, DASH), (0.4, "0.4 ms"), (9.96, "10.0 ms"), (24.4, "24 ms")]
)
def test_fmt_ms(value, text) -> None:
    assert fmt_ms(value) == text


def test_small_formatters() -> None:
    assert fmt_pct(None) == DASH and fmt_pct(0) == "0%" and fmt_pct(0.5) == "0.5%"
    assert fmt_pct(33.33) == "33%"
    assert fmt_dbm(-52) == "-52 dBm" and fmt_dbm(None) == DASH
    assert fmt_countdown(0) == "0:00"
    assert fmt_countdown(4.2) == "0:05"
    assert fmt_countdown(299.5) == "5:00"
    assert fmt_countdown(3725) == "1:02:05"
    assert fmt_countdown(-3) == "0:00"


def test_fmt_age() -> None:
    assert fmt_age(T0, T0 + timedelta(seconds=10)) == "just now"
    assert fmt_age(T0, T0 + timedelta(seconds=50)) == "1 min ago"
    assert fmt_age(T0, T0 + timedelta(minutes=12)) == "12 min ago"
    assert fmt_age(T0, T0 + timedelta(hours=5)) == "5 h ago"
    assert fmt_age(T0, T0 + timedelta(days=3)) == "3 d ago"


def test_fmt_clock() -> None:
    tz = timezone(timedelta(hours=2))
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)  # a Tuesday
    assert fmt_clock(now - timedelta(minutes=5), now, tz) == "13:55"
    assert fmt_clock(now - timedelta(days=1), now, tz) == "Mon 14:00"
    assert fmt_clock(now - timedelta(days=10), now, tz) == "2026-09-19 14:00"


def test_texts() -> None:
    assert reasons_text([InstabilityReason.HIGH_LOSS, InstabilityReason.HIGH_JITTER]) == (
        "High packet loss, high jitter"
    )
    assert score_text(Score(87, False)) == "87 · Excellent"
    assert score_text(Score(72, True)) == "~72 · Good"
    assert score_text(None) == DASH
    assert state_detail(RouterState.UNKNOWN, location_allowed=False) == "location access is off"
    assert state_detail(RouterState.VISIBLE, location_allowed=True) == "in range, not tested"


def test_busy_text() -> None:
    e = entry("11-11-11-11-11-11", load=BssLoad(6, 51))
    assert busy_text(Busyness(0.2, BusyLevel.LOW, False), e) == "Low (6 devices, 20% airtime)"
    assert busy_text(Busyness(0.5, BusyLevel.MEDIUM, True), None) == "Medium (estimated)"
    assert busy_text(None) == DASH


def test_recommendation_text() -> None:
    routers = sample_routers()
    rec = Recommendation("nb", Score(90, True), Score(70, False), 20)
    assert recommendation_text(rec, routers) == "Neighbor is 20 points better (90)"
    rec = Recommendation("nb", Score(90, True), None, None)
    assert recommendation_text(rec, routers) == "Neighbor has the best score (90)"


def test_current_metrics_and_targets_text() -> None:
    report, _ = run()
    m = current_metrics(report)
    assert m.gateway_ms == 2.0 and m.internet_ms == pytest.approx(10.5)
    assert m.rssi == -50 and m.loss_pct == 0
    lines = targets_text(report).splitlines()
    assert lines[0] == "1.1.1.1: 10 ms, 0% loss"
    assert lines[2].startswith("google.com: 10 ms, 0% loss, DNS 12 ms")
    assert current_metrics(None) is None and targets_text(None) == ""


def test_tray_tooltip_fits_windows_limit() -> None:
    report, _ = run()
    status = overall_status(report)
    text = tray_tooltip(report, status, checking=True)
    assert text.splitlines()[:2] == ["Router Checker", "ZTE: Stable"]
    assert "Gateway 2.0 ms" in text and text.endswith("Checking now…")
    long_name = replace(report.match.router, name="X" * 200)
    report.match = replace(report.match, router=long_name)
    assert len(tray_tooltip(report, status)) <= TOOLTIP_MAX
    assert tray_tooltip(None, overall_status(None)) == "Router Checker\nWaiting for the first check"


def test_group_networks() -> None:
    entries = [
        entry("AA-AA-AA-AA-AA-01", "Home", rssi=-60),
        entry("AA-AA-AA-AA-AA-02", "Home", rssi=-40, freq=5180, channel=36, band=Band.GHZ_5),
        entry("BB-BB-BB-BB-BB-01", "Cafe", rssi=-70),
        entry("CC-CC-CC-CC-CC-01", "", rssi=-30),  # hidden network
    ]
    zte = Router.create("ZTE", ssid="Home")
    nets = group_networks(entries, profiles=["Home"], routers=[zte])
    assert [n.ssid for n in nets] == ["Home", "Cafe"]
    home = nets[0]
    assert home.best_rssi == -40
    assert [str(b) for b in home.bssids] == ["AA-AA-AA-AA-AA-02", "AA-AA-AA-AA-AA-01"]
    assert home.bands == (Band.GHZ_2_4, Band.GHZ_5)
    assert home.has_profile and home.router_name == "ZTE"
    assert not nets[1].has_profile and nets[1].router_name is None
