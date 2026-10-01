import threading
from dataclasses import replace
from datetime import timedelta

import pytest

from fakes import (
    CABLE_IP,
    GW,
    MIDDLE,
    MIDDLE_LAN,
    NB_ADDRESS,
    NB_BSSID,
    NB_GW,
    NB_LAN,
    T0,
    WIFI_IP,
    ZTE_ADDRESS,
    ZTE_BSSID,
    ZTE_LAN,
    FakeNetInfo,
    FakeWifi,
    cable_gateway,
    mac,
    make_engine,
    middle_parts,
    middle_routers,
    neighbor_connection,
    record,
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


# --- behind a middle router -------------------------------------------------------------


def behind(upstream=ZTE_ADDRESS, **settings):
    s = Settings(
        pings_per_target=4, routers=middle_routers(), middle_router=f"{MIDDLE}:8080", **settings
    )
    return make(s, **middle_parts(upstream))


def test_behind_the_middle_router_the_router_in_use_is_found_one_hop_further() -> None:
    engine, parts = behind()
    report = engine.run_cycle()
    assert report.middle is not None and report.upstream_ip == ZTE_ADDRESS
    assert report.match.router.id == "zte"
    assert report.match.method is MatchMethod.ADDRESS
    assert report.connection is None
    assert parts["ping"].hops[0] == ("1.1.1.1", 2)  # toward the first target, 2 hops
    test = report.test
    assert test.gateway_ping.median_ms == 2.0  # the ZTE, not the middle router
    assert test.middle_ping.median_ms == 1.0
    assert set(parts["ping"].sources) == {CABLE_IP}
    assert report.record.link is LinkKind.ETHERNET and report.record.verdict is Verdict.OK
    by_id = {s.router.id: s for s in report.statuses}
    assert by_id["zte"].state is RouterState.ONLINE and by_id["zte"].is_current


def test_behind_the_middle_router_switching_advice_is_given() -> None:
    engine, parts = behind()
    for minutes in (0, 6):  # the Neighbor measured much better in a Test all
        parts["clock"].current = T0 + timedelta(minutes=minutes)
        parts["store"].add_check(
            record(router_id="nb", timestamp=T0 + timedelta(minutes=minutes), score=95.0)
        )
    parts["ping"].replies["1.1.1.1"] = [150.0, None, 190.0, 150.0]  # the ZTE is poor now
    parts["clock"].current = T0 + timedelta(minutes=10)
    report = engine.run_cycle()
    assert report.recommendation is not None and report.recommendation.router_id == "nb"


def test_an_unknown_router_behind_the_middle_router() -> None:
    engine, _ = behind(upstream="172.16.0.1")
    report = engine.run_cycle()
    assert report.upstream_ip == "172.16.0.1"
    assert report.match.router is None
    assert report.test.gateway_ping.median_ms is None  # 172.16.0.1 doesn't answer the fake


def test_when_the_second_hop_stays_silent_the_middle_router_is_pinged() -> None:
    engine, parts = behind(upstream=None)
    report = engine.run_cycle()
    assert report.upstream_ip is None and report.match.router is None
    assert report.test.gateway_ping.median_ms == 1.0 and report.test.middle_ping is None
    assert len(parts["ping"].hops) == 2  # tried twice


def test_with_only_domain_targets_the_hop_goes_toward_the_first_one() -> None:
    engine, parts = behind(targets=("google.com",))
    report = engine.run_cycle()
    assert parts["ping"].hops[0] == ("142.250.0.1", 2)
    assert report.match.router.id == "zte"


def test_a_middle_router_elsewhere_changes_nothing(routers) -> None:
    s = Settings(pings_per_target=4, routers=routers, middle_router=f"{MIDDLE}:8080")
    engine, parts = make(s)  # on the ZTE's Wi-Fi, not behind the middle router
    report = engine.run_cycle()
    assert report.middle is None and parts["ping"].hops == []
    assert report.match.method is MatchMethod.GATEWAY_MAC


def test_a_router_switched_to_learns_its_address_and_wifi_mac() -> None:
    zte, neighbor, gone = middle_routers()
    fresh = replace(neighbor, address=None)
    s = Settings(pings_per_target=4, routers=(zte, fresh, gone), middle_router=MIDDLE)
    engine, parts = make(s, **middle_parts(NB_ADDRESS))
    gateway = parts["netinfo"].gateway()
    record, learned = engine.test_behind(fresh, gateway, NB_ADDRESS, mac(NB_BSSID))
    assert record.router_id == "nb" and record.gateway_avg_ms == 4.0  # pinged the Neighbor
    assert (learned.address, learned.middle_bssid) == (NB_ADDRESS, mac(NB_BSSID))
    assert engine.settings.router("nb").address == NB_ADDRESS
    assert parts["store"].events("nb", 1)[0].message == (
        f"Neighbor is at {NB_ADDRESS} behind the middle router"
    )
    assert engine.test_behind(learned, gateway, NB_ADDRESS, mac(NB_BSSID))[1] is None


def test_two_routers_with_the_same_address_cant_be_told_apart() -> None:
    zte, neighbor, gone = middle_routers()
    s = Settings(pings_per_target=4, routers=(zte, neighbor, gone), middle_router=MIDDLE)
    engine, parts = make(s, **middle_parts(ZTE_ADDRESS))
    engine.test_behind(neighbor, parts["netinfo"].gateway(), ZTE_ADDRESS, None)
    assert "so is ZTE, so Router Checker can't tell them apart" in (
        parts["store"].events("nb", 1)[0].message
    )
    report = engine.run_cycle()
    assert report.match.router is None  # both are at that address now


def test_behind_the_middle_router_the_others_show_their_last_test_through_it() -> None:
    engine, parts = behind()
    parts["store"].add_check(record(router_id="nb", timestamp=T0 - timedelta(minutes=12)))
    report = engine.run_cycle()
    by_id = {s.router.id: s for s in report.statuses}
    assert by_id["nb"].state is RouterState.TESTED
    assert by_id["nb"].last_tested == T0 - timedelta(minutes=12)
    assert by_id["gone"].state is RouterState.NOT_TESTED and by_id["gone"].last_tested is None
    # This PC's scan shows the Neighbor, but that's not how the middle router sees it.
    assert all(s.observation is None for s in report.statuses)
    assert by_id["zte"].last_tested is None


# --- spotting a middle router that isn't set up -------------------------------------------


def unset(**settings):
    """On a cable into the middle router, which isn't set up in the app."""
    return make(
        Settings(pings_per_target=4, routers=middle_routers(), **settings), **middle_parts()
    )


def test_a_middle_router_that_isnt_set_up_is_suggested() -> None:
    engine, parts = unset()
    report = engine.run_cycle()
    assert report.middle is None and report.match.router is None
    assert report.middle_suggestion == MIDDLE
    assert parts["ping"].hops[0] == ("1.1.1.1", 2)
    assert parts["middle"].calls == []  # nothing is sent to it before you confirm


def test_a_dismissed_suggestion_isnt_looked_for_again() -> None:
    engine, parts = unset(middle_dismissed=(MIDDLE_LAN,))
    report = engine.run_cycle()
    assert report.middle_suggestion is None and parts["ping"].hops == []


def test_a_cable_straight_into_your_router_suggests_nothing(routers) -> None:
    engine, parts = make(Settings(pings_per_target=4, routers=routers))
    parts["netinfo"].cable = cable_gateway(GW, ZTE_LAN)
    report = engine.run_cycle()
    assert report.middle_suggestion is None and parts["ping"].hops == []


def test_a_public_second_hop_suggests_nothing() -> None:
    engine, parts = unset()
    parts["ping"].upstream = "104.186.104.1"
    assert engine.run_cycle().middle_suggestion is None
