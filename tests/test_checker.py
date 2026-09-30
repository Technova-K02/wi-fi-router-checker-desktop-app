import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from fakes import (
    CABLE_IP,
    GW,
    NB_BSSID,
    NB_GW,
    NB_LAN,
    T0,
    WIFI_IP,
    ZTE_BSSID,
    ZTE_LAN,
    FakeNetInfo,
    FakeWifi,
    cable_gateway,
    mac,
    make_engine,
    neighbor_connection,
    sample_routers,
)
from router_checker.core.errors import CheckCancelled
from router_checker.core.matching import MatchMethod
from router_checker.core.models import (
    AlertKind,
    BusyLevel,
    LinkChoice,
    LinkKind,
    Router,
    RouterState,
    Verdict,
)
from router_checker.core.settings import Settings

make = make_engine


@pytest.fixture
def routers():
    return sample_routers()


def test_full_cycle(routers) -> None:
    engine, _ = make(Settings(pings_per_target=4, routers=routers))
    report = engine.run_cycle()

    assert report.location_allowed
    assert report.match.router.id == "zte"
    assert report.match.method is MatchMethod.GATEWAY_MAC
    assert report.match.macs_to_link == (mac(ZTE_BSSID),)  # learns its Wi-Fi BSSID
    assert report.record.verdict is Verdict.OK
    assert [t.address for t in report.test.targets] == ["1.1.1.1", "8.8.8.8", "142.250.0.1"]
    assert report.test.targets[2].dns.elapsed_ms == 12.0
    assert report.test.rssi == -50  # from the scan entry of the connected BSSID

    by_id = {s.router.id: s for s in report.statuses}
    assert by_id["zte"].state is RouterState.ONLINE and by_id["zte"].is_current
    assert by_id["zte"].score is not None and not by_id["zte"].score.estimated
    assert by_id["nb"].state is RouterState.VISIBLE
    assert by_id["nb"].score.estimated
    assert by_id["gone"].state is RouterState.NOT_FOUND and by_id["gone"].score is None

    zte_obs = by_id["zte"].observation
    assert zte_obs.entry.bssid == mac(ZTE_BSSID)
    assert zte_obs.same_channel_count == 1
    assert zte_obs.busyness.level is BusyLevel.LOW and not zte_obs.busyness.estimated

    assert not report.unstable
    assert (report.next_check_at - report.timestamp).total_seconds() == 300


def test_links_macs_when_matched_by_ssid() -> None:
    home = Router("home", "Home", "#0078D4", ssid="ZTE-Home")
    engine, parts = make(Settings(pings_per_target=4, routers=(home,)))
    report = engine.run_cycle()
    assert report.match.method is MatchMethod.SSID
    linked = report.settings.router("home")
    assert set(linked.macs) == {mac(ZTE_LAN), mac(ZTE_BSSID)}
    assert engine.settings.router("home") == linked
    assert parts["store"].events("home", 5)[0].kind == "linked"

    # Without location permission it is now found by gateway MAC.
    parts["wifi"].denied = True
    report = engine.run_cycle()
    assert not report.location_allowed
    assert report.match.router.id == "home" and report.match.method is MatchMethod.GATEWAY_MAC
    assert report.statuses[0].state is RouterState.ONLINE


def test_no_location_permission(routers) -> None:
    engine, _ = make(Settings(pings_per_target=4, routers=routers), wifi=FakeWifi(denied=True))
    report = engine.run_cycle()
    assert not report.location_allowed
    assert report.scan is None
    states = {s.router.id: s.state for s in report.statuses}
    assert states == {
        "zte": RouterState.ONLINE,
        "nb": RouterState.UNKNOWN,
        "gone": RouterState.UNKNOWN,
    }
    assert report.test.rssi is None


def test_not_connected(routers) -> None:
    engine, parts = make(Settings(routers=routers), netinfo=FakeNetInfo(None))
    report = engine.run_cycle()
    assert report.test is None and report.record is None
    assert parts["ping"].calls == []


def test_unstable_alerts_speed_up_and_recover(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    ping, clock, notifier = parts["ping"], parts["clock"], parts["notifier"]

    ping.replies[GW] = [2.0, None]  # 50 % gateway loss
    first = engine.run_cycle()
    assert first.record.verdict is Verdict.UNSTABLE and first.alert is None
    assert first.unstable
    assert (first.next_check_at - first.timestamp).total_seconds() == 60

    clock.advance(1)
    second = engine.run_cycle()
    assert second.alert is not None and second.alert.kind is AlertKind.UNSTABLE
    assert [a.kind for a in notifier.alerts] == [AlertKind.UNSTABLE]

    ping.replies[GW] = [2.0]
    for _ in range(2):
        clock.advance(1)
        last = engine.run_cycle()
    assert last.alert is not None and last.alert.kind is AlertKind.RECOVERED
    assert not last.unstable
    assert [e.kind for e in parts["store"].events("zte", 10)][:2] == ["recovered", "unstable"]


def test_internet_down_is_not_blamed_on_router(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["ping"].replies = {GW: [2.0]}
    report = engine.run_cycle()
    assert report.record.verdict is Verdict.INTERNET_DOWN


def test_notifications_can_be_disabled(routers) -> None:
    settings = Settings(
        pings_per_target=4, routers=routers, notifications_enabled=False, unstable_checks=1
    )
    engine, parts = make(settings)
    parts["ping"].replies[GW] = [None, 2.0]
    assert engine.run_cycle().alert is not None
    assert parts["notifier"].alerts == []


def test_recommendation_from_measured_scores(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["ping"].replies = {GW: [2.0], "1.1.1.1": [150.0, None, 190.0, 150.0]}
    parts["dns"].table = {}
    report = engine.run_cycle()
    by_id = {s.router.id: s for s in report.statuses}
    assert by_id["zte"].score.value < by_id["nb"].score.value - 10
    assert report.recommendation is not None and report.recommendation.router_id == "nb"
    assert by_id["nb"].recommended and not by_id["zte"].recommended


def test_settings_update_applies_to_alert_engine(routers) -> None:
    engine, _ = make(Settings(routers=routers))
    engine.settings = replace(engine.settings, unstable_checks=3, alert_cooldown_min=30)
    assert engine._alerts.unstable_checks == 3
    assert engine._alerts.cooldown.total_seconds() == 1800


def test_scores_are_stored_for_every_scored_router(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    report = engine.run_cycle()
    store = parts["store"]
    since = report.timestamp
    zte_points = store.scores("zte", since)
    assert [(p.value, p.estimated) for p in zte_points] == [(report.statuses[0].score.value, False)]
    assert store.scores("nb", since)[0].estimated
    assert store.scores("gone", since) == []  # not seen, no score


def test_confirmed_and_recovering_flags(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    ping, clock = parts["ping"], parts["clock"]
    ping.replies[GW] = [2.0, None]
    first = engine.run_cycle()
    assert not first.confirmed_unstable and not first.recovering
    clock.advance(1)
    assert engine.run_cycle().confirmed_unstable
    ping.replies[GW] = [2.0]
    clock.advance(1)
    third = engine.run_cycle()
    assert third.recovering and not third.confirmed_unstable
    clock.advance(1)
    assert not engine.run_cycle().recovering


def test_target_the_ping_service_rejects_counts_as_lost(routers) -> None:
    class PickyPing:
        def ping(self, address, count, timeout_ms, spacing_ms, stop=None, source=None):
            if address == "8.8.8.8":
                raise OSError("unsupported address")
            return [2.0] * count

    engine, _ = make(Settings(pings_per_target=4, routers=routers), ping=PickyPing())
    report = engine.run_cycle()
    by_target = {t.target: t for t in report.test.targets}
    assert by_target["8.8.8.8"].ping.all_lost
    assert not by_target["1.1.1.1"].ping.all_lost


def test_a_stopped_check_raises_and_saves_nothing(routers) -> None:
    settings = Settings(pings_per_target=4, routers=routers)
    engine, parts = make(settings)
    fake_ping = parts["ping"]
    real_ping = fake_ping.ping
    exiting = threading.Event()

    def ping_then_exit(*args, **kwargs):
        exiting.set()  # the user exits while the pings run
        return real_ping(*args, **kwargs)

    fake_ping.ping = ping_then_exit
    with pytest.raises(CheckCancelled):
        engine.run_cycle(stop=exiting)

    store = parts["store"]
    assert store.recent_checks(10) == []
    assert store.events(None, 10) == []  # not even the Wi-Fi MAC it would have linked
    assert store.scores("zte", T0 - timedelta(days=1)) == []
    assert engine.settings.router("zte") == settings.router("zte")

    fake_ping.ping = real_ping
    report = engine.run_cycle()  # the next check runs normally
    assert report.record.verdict is Verdict.OK
    assert [r.id for r in report.linked] == ["zte"]


def test_an_unstable_alert_names_a_better_router(routers) -> None:
    settings = Settings(pings_per_target=4, routers=routers, unstable_checks=1)
    engine, parts = make(settings)
    parts["ping"].replies[GW] = [2.0, None]  # half the pings to the router get lost
    report = engine.run_cycle()
    assert report.alert.kind is AlertKind.UNSTABLE
    assert report.recommendation.router_id == "nb"
    assert report.alert.recommended_name == "Neighbor"
    assert report.alert.recommended_id == "nb"
    assert report.alert.recommended_score == report.recommendation.score
    event = parts["store"].events("zte", 1)[0]
    assert event.message.endswith(report.alert.suggestion)


def test_testing_another_router_saves_a_check_but_never_alerts(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers, unstable_checks=1))
    parts["ping"].replies[NB_GW] = [4.0, None]  # half the pings to its gateway get lost
    record, linked = engine.test_other(routers[1], *neighbor_connection())
    assert (record.router_id, record.ssid, record.verdict) == ("nb", "Neighbor", Verdict.UNSTABLE)
    assert record.rssi == -65  # from the signal quality: there is no scan
    assert parts["notifier"].alerts == [] and parts["store"].events("nb", 5)[0].kind == "linked"
    assert set(linked.macs) == {mac(NB_LAN), mac(NB_BSSID)}
    assert engine.settings.router("nb") == linked
    assert parts["store"].recent_checks(5, "nb") == [record]
    assert parts["store"].hourly("nb", T0)[0].score_avg == pytest.approx(record.score)


def test_testing_another_router_can_be_stopped(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    stop = threading.Event()
    stop.set()
    with pytest.raises(CheckCancelled):
        engine.test_other(routers[1], *neighbor_connection(), stop=stop)
    assert parts["store"].recent_checks(5) == [] and parts["store"].events(None, 5) == []


# --- Ethernet and VPN ---------------------------------------------------------------------


def test_on_a_cable_the_router_is_found_by_its_lan_mac(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["netinfo"].cable = cable_gateway()  # Wi-Fi is connected too
    report = engine.run_cycle()
    assert report.gateway.wired
    assert report.connection is None  # the Wi-Fi connection isn't what's checked
    assert report.match.router.id == "zte"
    assert report.match.method is MatchMethod.GATEWAY_MAC
    assert report.match.macs_to_link == ()  # no Wi-Fi BSSID to learn over a cable
    record = report.record
    assert (record.link, record.ssid, record.rssi, record.signal_quality) == (
        LinkKind.ETHERNET, None, None, None
    )  # fmt: skip
    assert record.verdict is Verdict.OK and record.score is not None  # scored without signal
    assert set(parts["ping"].sources) == {CABLE_IP}  # every ping leaves through the cable
    by_id = {s.router.id: s for s in report.statuses}
    assert by_id["zte"].state is RouterState.ONLINE
    assert by_id["nb"].state is RouterState.VISIBLE  # the Wi-Fi scan still sees the others


def test_on_wifi_the_pings_leave_through_wifi(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    report = engine.run_cycle()
    assert report.record.link is LinkKind.WIFI
    assert set(parts["ping"].sources) == {WIFI_IP}


def test_the_connection_setting_is_passed_on(routers) -> None:
    engine, parts = make(Settings(routers=routers, connection=LinkChoice.WIFI))
    parts["netinfo"].cable = cable_gateway()
    report = engine.run_cycle()
    assert parts["netinfo"].choices == [LinkChoice.WIFI]
    assert report.record.link is LinkKind.WIFI
    assert report.connection is not None


def test_a_pc_without_wifi_checks_its_cable(routers) -> None:
    wifi = FakeWifi(missing=True)
    engine, _ = make(
        Settings(pings_per_target=4, routers=routers),
        wifi=wifi, netinfo=FakeNetInfo(cable=cable_gateway()),
    )  # fmt: skip
    report = engine.run_cycle()
    assert report.wifi_error == "No Wi-Fi adapter found."
    assert report.scan is None
    assert report.match.router.id == "zte"
    assert report.record.link is LinkKind.ETHERNET and report.record.verdict is Verdict.OK


TARGETS = ("1.1.1.1", "8.8.8.8", "142.250.0.1")


def test_with_a_vpn_the_router_is_checked_past_it(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["netinfo"].vpn = True
    report = engine.run_cycle()
    assert report.gateway.vpn
    assert report.record.verdict is Verdict.OK
    assert not report.record.via_vpn
    assert set(parts["ping"].sources) == {WIFI_IP}


def test_a_vpn_that_blocks_other_traffic_isnt_an_internet_problem(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["netinfo"].vpn = True
    parts["ping"].blocked = {(WIFI_IP, t) for t in TARGETS}  # the VPN's kill switch
    report = engine.run_cycle()
    assert report.record.verdict is Verdict.OK
    assert report.record.via_vpn
    assert report.record.internet_loss_pct == 0
    # Tried past the VPN first, then through it.
    assert parts["ping"].sources.count(None) == len(TARGETS)


def test_without_a_vpn_failing_targets_are_an_internet_problem(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["ping"].blocked = {(WIFI_IP, t) for t in TARGETS}
    report = engine.run_cycle()
    assert report.record.verdict is Verdict.INTERNET_DOWN
    assert not report.record.via_vpn
    assert None not in parts["ping"].sources  # no second try


def test_with_a_vpn_nothing_answering_at_all_is_still_an_internet_problem(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["netinfo"].vpn = True
    parts["ping"].blocked = {(src, t) for t in TARGETS for src in (WIFI_IP, None)}
    report = engine.run_cycle()
    assert report.record.verdict is Verdict.INTERNET_DOWN
    assert not report.record.via_vpn
