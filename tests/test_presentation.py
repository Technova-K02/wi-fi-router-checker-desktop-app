from dataclasses import replace
from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from fakes import (
    GW,
    T0,
    FakeNetInfo,
    FakeWifi,
    entry,
    gateway_info,
    make_engine,
    neighbor_connection,
    network_parts,
    sample_routers,
)
from router_checker.core.models import (
    Band,
    BssLoad,
    BusyLevel,
    Busyness,
    HourlyAggregate,
    InstabilityReason,
    Recommendation,
    Router,
    RouterState,
    Score,
    Verdict,
    WifiConnection,
)
from router_checker.core.popularity import popular_times
from router_checker.core.presentation import (
    DASH,
    TOOLTIP_MAX,
    StatusLevel,
    busy_text,
    confirm_test_all,
    current_metrics,
    fmt_age,
    fmt_clock,
    fmt_countdown,
    fmt_dbm,
    fmt_duration,
    fmt_ms,
    fmt_pct,
    group_networks,
    join_names,
    overall_status,
    popular_cell_text,
    popular_times_summary,
    progress_steps,
    progress_text,
    quiet_hours_text,
    reasons_text,
    recommendation_text,
    score_text,
    state_detail,
    summarize_test_all,
    switch_summary,
    targets_text,
    tray_tooltip,
    verdict_level,
)
from router_checker.core.quiet_hours import QuietHours
from router_checker.core.settings import Settings
from router_checker.core.switching import (
    LOCATION_BLOCKER,
    NO_CONNECTION,
    Outcome,
    Progress,
    Stage,
    SwitchResult,
    TestAllPlan,
    TestAllResult,
    gather_plan,
    plan_test_all,
)

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


def test_popular_times_texts() -> None:
    tuesday_19 = datetime(2026, 9, 29, 19, tzinfo=UTC)

    def aggs(hours: int, value: float) -> list[HourlyAggregate]:
        return [
            HourlyAggregate("r", tuesday_19 + timedelta(hours=h), value, None) for h in range(hours)
        ]

    assert popular_times_summary(popular_times([], UTC)).startswith("No data yet.")
    assert popular_times_summary(popular_times(aggs(5, 0.9), UTC)) == (
        "Not enough data yet (5 hours so far). This fills in over the week."
    )
    quiet = popular_times(aggs(30, 0.1), UTC)
    assert popular_times_summary(quiet) == "Rarely busy: Low at every hour recorded so far."
    busy = popular_times([*aggs(1, 0.9), *aggs(30, 0.1)[1:]], UTC)
    assert popular_times_summary(busy) == "Usually busiest on Tuesdays, 19:00–20:00 (High)."

    again = popular_times(
        [*aggs(1, 0.9), HourlyAggregate("r", tuesday_19 + timedelta(days=7), 0.5, None)], UTC
    )
    assert popular_cell_text(again, 1, 19) == ("Tuesday 19:00–20:00: High (average of 2 Tuesdays)")
    assert popular_cell_text(again, 1, 20) == "Tuesday 20:00–21:00: no data yet"


def test_quiet_hours_text() -> None:
    assert quiet_hours_text(QuietHours()).startswith("Off.")
    assert quiet_hours_text(QuietHours(True)) == "No notifications from 22:00 to 07:00."
    assert "nothing is muted" in quiet_hours_text(QuietHours(True, time(8), time(8)))


def test_names_and_durations() -> None:
    assert join_names([]) == "" and join_names(["A"]) == "A"
    assert join_names(["A", "B"]) == "A and B" and join_names(["A", "B", "C"]) == "A, B and C"
    assert fmt_duration(50) == "about a minute" and fmt_duration(80) == "about 2 minutes"


def sample_plan():
    parts = network_parts()
    routers = sample_routers()
    return gather_plan(routers, parts["wifi"], parts["netinfo"], parts["switcher"]), routers, parts


def test_confirm_test_all() -> None:
    plan, routers, parts = sample_plan()
    question = confirm_test_all(plan)
    assert question.title == "Test all routers?"
    assert question.text.splitlines() == [
        "Router Checker connects to each router in turn, tests it, then reconnects to "
        "“ZTE-Home”. Your internet drops for a few seconds at each switch. "
        "This takes about a minute.",
        "",
        "To test: Neighbor and ZTE (you're on it, so no switch).",
        "Skipped:",
        "• Gone: Not in range right now.",
    ]
    blocked = confirm_test_all(TestAllPlan(None, None, blocker=LOCATION_BLOCKER))
    assert (blocked.title, blocked.text) == ("Test all can't run", LOCATION_BLOCKER)
    nothing = confirm_test_all(plan_test_all(routers, None, None, [], []))
    assert nothing.title == "No other router to test"
    assert "• ZTE: No Wi-Fi name yet. Add it with Edit." in nothing.text.splitlines()
    scan, saved = parts["wifi"].entries, parts["switcher"].saved
    off_wifi = confirm_test_all(plan_test_all(routers, None, None, scan, saved))
    assert "then disconnects Wi-Fi again." in off_wifi.text


def test_progress_texts_and_steps() -> None:
    connecting = Progress(Stage.CONNECTING, "Cafe", 2, 3)
    testing = Progress(Stage.TESTING, "Cafe", 2, 3)
    restoring = Progress(Stage.RESTORING, "ZTE-Home", 4, 3)
    assert progress_text(None) == "Starting Test all…"
    assert progress_text(connecting) == "Connecting to Cafe (2 of 3)…"
    assert progress_text(testing) == "Testing Cafe (2 of 3)…"
    assert progress_text(restoring) == "Reconnecting to “ZTE-Home”…"
    steps = [progress_steps(p, 3) for p in (None, connecting, testing, restoring)]
    assert steps == [(0, 8), (2, 8), (3, 8), (6, 8)]


def test_test_all_summary() -> None:
    engine, parts = make_engine(Settings(pings_per_target=4, routers=sample_routers()))
    _, nb, _ = engine.settings.routers
    plan = gather_plan(engine.settings.routers, parts["wifi"], parts["netinfo"], parts["switcher"])
    nb_record, _ = engine.test_other(nb, *neighbor_connection())
    report = engine.run_cycle()  # the final check, back on the ZTE
    scores = {s.router.id: s.score.value for s in report.statuses if s.score}
    assert scores["zte"] > scores["nb"]

    done = TestAllResult(plan, (Outcome(nb, nb_record),), restored=True, cancelled=False)
    summary = summarize_test_all(done, report)
    assert (summary.level, summary.title) == (L.GOOD, "Test all finished")
    assert summary.text == (
        f"Tested Neighbor and ZTE. Best: ZTE (the one you're on), score {scores['zte']}. "
        "Back on “ZTE-Home”."
    )
    cancelled = replace(done, outcomes=(Outcome(nb, None, NO_CONNECTION),), cancelled=True)
    summary = summarize_test_all(cancelled, report)
    assert (summary.level, summary.title) == (L.WARNING, "Test all cancelled")
    assert "Couldn't connect to Neighbor (no connection within 20 seconds)." in summary.text

    stuck = summarize_test_all(replace(done, restored=False), report)
    assert (stuck.level, stuck.title) == (L.BAD, "Couldn't reconnect to “ZTE-Home”")
    assert stuck.restore_failed and "Wi-Fi menu on the taskbar" in stuck.text
    off = replace(done, plan=replace(plan, origin=None), restored=False)
    assert summarize_test_all(off, report).title == "Wi-Fi is still connected"
    failed = summarize_test_all(None, report, "OSError: boom")
    assert (failed.title, failed.text) == (
        "Test all stopped",
        "OSError: boom. You're on “ZTE-Home”.",
    )


def test_switch_summary() -> None:
    _, nb, _ = sample_routers()
    origin = WifiConnection("ZTE-Home", None, 90, "ZTE-Home")
    ok = switch_summary(SwitchResult(nb, True, origin=origin))
    assert (ok.level, ok.title, ok.text) == (L.GOOD, "Switched to Neighbor", "Checking it now.")
    blocked = switch_summary(SwitchResult(nb, False, "Not in range right now."))
    assert (blocked.title, blocked.text) == ("Can't switch to Neighbor", "Not in range right now.")
    back = switch_summary(SwitchResult(nb, False, NO_CONNECTION, True, origin))
    assert back.text == "No connection within 20 seconds. Back on “ZTE-Home”."
    stuck = switch_summary(SwitchResult(nb, False, NO_CONNECTION, False, origin))
    assert stuck.level is L.BAD and stuck.restore_failed
    assert "Couldn't reconnect to “ZTE-Home” either." in stuck.text


def test_tray_tooltip_shows_the_activity() -> None:
    report, _ = run()
    text = tray_tooltip(report, overall_status(report), True, "Testing Cafe (2 of 3)…")
    assert text.endswith("Testing Cafe (2 of 3)…") and "Checking now" not in text
