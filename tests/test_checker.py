from dataclasses import replace

import pytest

from fakes import (
    FakeClock,
    FakeDns,
    FakeNetInfo,
    FakeNotifier,
    FakePing,
    FakeWifi,
    entry,
    gateway_info,
    mac,
)
from router_checker.core.checker import CheckEngine
from router_checker.core.matching import MatchMethod
from router_checker.core.models import (
    AlertKind,
    Band,
    BssLoad,
    BusyLevel,
    Router,
    RouterState,
    Verdict,
    WifiConnection,
)
from router_checker.core.settings import Settings
from router_checker.core.storage import SqliteHistoryStore

GW = "192.168.1.1"
ZTE_LAN = "B0-0A-D5-9A-7B-B4"
ZTE_BSSID = "B0-0A-D5-9A-7B-B8"
GOOD = [10.0, 12.0, 11.0, 10.0]


def make(settings: Settings, **overrides):
    parts = {
        "wifi": FakeWifi(
            connection=WifiConnection("ZTE-Home", mac(ZTE_BSSID), 90),
            entries=[
                entry(
                    ZTE_BSSID,
                    "ZTE-Home",
                    rssi=-50,
                    freq=5180,
                    channel=36,
                    band=Band.GHZ_5,
                    load=BssLoad(2, 20),
                ),
                entry("22-22-22-22-22-22", "Neighbor", rssi=-60),
                entry(
                    "33-33-33-33-33-33", "Cafe", rssi=-70, channel=36, freq=5180, band=Band.GHZ_5
                ),
            ],
        ),
        "ping": FakePing({GW: [2.0], "1.1.1.1": GOOD, "8.8.8.8": GOOD, "142.250.0.1": GOOD}),
        "dns": FakeDns({"google.com": "142.250.0.1"}),
        "netinfo": FakeNetInfo(gateway_info(GW, ZTE_LAN)),
        "store": SqliteHistoryStore(":memory:"),
        "clock": FakeClock(),
        "notifier": FakeNotifier(),
    }
    parts.update(overrides)
    return CheckEngine(settings, **parts), parts


@pytest.fixture
def routers():
    zte = Router("zte", "ZTE", "#0078D4", macs=(mac(ZTE_LAN),))
    neighbor = Router("nb", "Neighbor", "#107C10", ssid="Neighbor")
    gone = Router("gone", "Gone", "#D83B01", ssid="Gone")
    return zte, neighbor, gone


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
