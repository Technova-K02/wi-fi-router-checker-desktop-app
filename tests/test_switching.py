"""Test all and the Switch button: planning, switching, always restoring."""

import threading
from datetime import timedelta

import pytest

from fakes import (
    CAFE_BSSID,
    NB_BSSID,
    NB_GW,
    NB_LAN,
    T0,
    ZTE_BSSID,
    FakeWifi,
    entry,
    gateway_info,
    mac,
    network_parts,
    sample_routers,
)
from router_checker.core.checker import CheckEngine
from router_checker.core.errors import WifiUnavailableError
from router_checker.core.models import Router, SavedNetwork, Verdict, WifiConnection
from router_checker.core.settings import Settings
from router_checker.core.switching import (
    LOCATION_BLOCKER,
    NO_CONNECTION,
    TEST_ALL_EVENT,
    MarkerFile,
    Recovery,
    RestoreMarker,
    SkipReason,
    Stage,
    SwitchRunner,
    TestAllPlan,
    gather_plan,
    plan_test_all,
    profile_for,
    recovery_for,
    usable_address,
    wifi_name,
)

CAFE = Router("cafe", "Cafe", "#8764B8", ssid="Cafe")


def cafe_connection():
    return WifiConnection("Cafe", mac(CAFE_BSSID), 60, "Cafe 2"), gateway_info("10.1.1.1", None)


@pytest.fixture
def parts():
    return network_parts()


def routers_with_cafe(parts):
    """ZTE (current), Neighbor and Cafe can be tested; Gone is out of range."""
    switcher = parts["switcher"]
    switcher.saved.append(SavedNetwork("Cafe 2", "Cafe"))  # a renamed profile
    switcher.networks["Cafe 2"] = cafe_connection()
    parts["ping"].replies["10.1.1.1"] = [8.0]
    return (*sample_routers(), CAFE)


def plan_for(parts, routers):
    return gather_plan(routers, parts["wifi"], parts["netinfo"], parts["switcher"])


def runner_for(parts, routers, tmp_path, **options):
    engine = CheckEngine(
        Settings(pings_per_target=4, routers=routers),
        wifi=parts["wifi"],
        ping=parts["ping"],
        dns=parts["dns"],
        netinfo=parts["netinfo"],
        store=parts["store"],
        clock=parts["clock"],
        notifier=parts["notifier"],
    )
    timing = {"connect_timeout_s": 0.05, "settle_s": 0.0, "poll_s": 0.001} | options
    marker = MarkerFile(tmp_path / "test-all-restore.json")
    runner = SwitchRunner(
        engine,
        parts["switcher"],
        parts["wifi"],
        parts["netinfo"],
        parts["store"],
        parts["clock"],
        marker,
        **timing,
    )
    return runner, engine, marker


def run(runner, plan, cancel=None, exiting=None, progress=None, scheduled=False):
    return runner.test_all(
        plan,
        cancel=cancel or threading.Event(),
        exiting=exiting or threading.Event(),
        progress=progress or (lambda _p: None),
        scheduled=scheduled,
    )


# --- planning -------------------------------------------------------------------------


def test_plan_tests_in_place_switches_and_skips(parts) -> None:
    office = Router("office", "Office", "#008575", ssid="Office")  # no saved profile
    unnamed = Router("unnamed", "Unnamed", "#C239B3", macs=(mac("44-44-44-44-44-44"),))
    routers = (*routers_with_cafe(parts), office, unnamed)
    plan = plan_for(parts, routers)
    assert plan.origin.ssid == "ZTE-Home" and plan.current.id == "zte"
    assert [(c.router.id, c.ssid, c.profile_name) for c in plan.to_test] == [
        ("nb", "Neighbor", "Neighbor"),
        ("cafe", "Cafe", "Cafe 2"),
    ]
    assert {s.router.id: s.reason for s in plan.skipped} == {
        "gone": SkipReason.NOT_IN_RANGE,
        "office": SkipReason.NO_PROFILE,
        "unnamed": SkipReason.NO_WIFI_NAME,
    }
    assert plan.can_run and plan.estimated_seconds == 2 * 30 + 20


def test_wifi_name_and_profile_matching() -> None:
    scan = network_parts()["wifi"].entries
    known_bssid = Router("x", "X", "#0078D4", macs=(mac(NB_BSSID),))
    assert wifi_name(known_bssid, scan) == "Neighbor"  # from its known Wi-Fi MAC
    assert wifi_name(Router("y", "Y", "#0078D4", macs=(mac(NB_LAN),)), scan) is None
    saved = [
        SavedNetwork("Home 5G", "Home"),
        SavedNetwork("Home", "Home"),
        SavedNetwork("Legacy", None),  # its Wi-Fi name couldn't be read
    ]
    assert profile_for("Home", saved) == "Home"  # the one named like the network first
    assert profile_for("Home", saved[:1]) == "Home 5G"
    assert profile_for("Legacy", saved) == "Legacy"
    assert profile_for("Other", saved) is None


def test_plan_skips_a_second_router_with_the_same_name(parts) -> None:
    zte, nb, gone = sample_routers()
    zte = zte.with_macs(mac(ZTE_BSSID))  # found by BSSID, so it stays the current router
    mesh = Router("mesh", "Mesh", "#008575", ssid="ZTE-Home")
    twin = Router("twin", "Twin", "#C239B3", ssid="Neighbor")
    plan = plan_for(parts, (zte, nb, gone, mesh, twin))
    assert plan.current.id == "zte"
    reasons = {s.router.id: s.reason for s in plan.skipped}
    assert reasons["mesh"] is SkipReason.SAME_NAME  # the network you're on
    assert reasons["twin"] is SkipReason.SAME_NAME  # Neighbor is tested already
    assert [c.router.id for c in plan.to_test] == ["nb"]


def test_a_hidden_network_is_in_range_by_its_bssid(parts) -> None:
    hidden = Router("hid", "Hidden", "#008575", ssid="Secret", macs=(mac("55-55-55-55-55-55"),))
    parts["wifi"].entries.append(entry("55-55-55-55-55-55", "", rssi=-65))  # no name broadcast
    parts["switcher"].saved.append(SavedNetwork("Secret", "Secret"))
    plan = plan_for(parts, (*sample_routers(), hidden))
    assert "hid" in [c.router.id for c in plan.to_test]


def test_what_blocks_test_all(parts) -> None:
    routers = sample_routers()
    denied = gather_plan(routers, FakeWifi(denied=True), parts["netinfo"], parts["switcher"])
    assert denied.blocker == LOCATION_BLOCKER and not denied.can_run

    no_profile = WifiConnection("Hotel", None, 80, profile_name="")
    plan = plan_test_all(routers, no_profile, None, parts["wifi"].entries, [])
    assert "Hotel" in plan.blocker and not plan.can_run

    class BrokenWifi(FakeWifi):
        def current_connection(self):
            raise WifiUnavailableError("the Wi-Fi radio is turned off")

    off = gather_plan(routers, BrokenWifi(), parts["netinfo"], parts["switcher"])
    assert off.blocker == "Wi-Fi isn't available: the Wi-Fi radio is turned off."

    nothing = plan_test_all(routers, None, None, [], [])
    assert nothing.blocker is None and not nothing.can_run and nothing.origin is None


def test_usable_address() -> None:
    assert usable_address("192.168.1.50")
    assert not usable_address("169.254.10.2")  # DHCP failed
    assert not usable_address("0.0.0.0")
    assert not usable_address(None) and not usable_address("junk")


# --- Test all ---------------------------------------------------------------------------


def test_test_all_tests_each_router_and_goes_back(parts, tmp_path) -> None:
    routers = routers_with_cafe(parts)
    runner, engine, marker = runner_for(parts, routers, tmp_path)
    plan = plan_for(parts, routers)
    seen = []
    markers = []
    parts["switcher"].on_connect = lambda _name: markers.append(marker.load())
    result = run(runner, plan, progress=seen.append)

    assert result.restored and not result.cancelled and not result.failed
    assert parts["switcher"].calls == ["connect Neighbor", "connect Cafe 2", "connect ZTE-Home"]
    assert parts["wifi"].connection.ssid == "ZTE-Home"
    assert [(p.stage, p.name, p.step, p.total) for p in seen] == [
        (Stage.CONNECTING, "Neighbor", 1, 2),
        (Stage.TESTING, "Neighbor", 1, 2),
        (Stage.CONNECTING, "Cafe", 2, 2),
        (Stage.TESTING, "Cafe", 2, 2),
        (Stage.RESTORING, "ZTE-Home", 3, 2),
    ]
    nb, cafe = (o.record for o in result.tested)
    assert (nb.router_id, nb.ssid, nb.verdict) == ("nb", "Neighbor", Verdict.OK)
    assert nb.gateway_avg_ms == 4.0 and cafe.gateway_avg_ms == 8.0
    assert NB_GW in parts["ping"].calls

    # Written before the first switch, gone once back.
    assert markers[0] == RestoreMarker(T0, "ZTE-Home", "ZTE-Home", ("Neighbor", "Cafe"))
    assert not marker.path.exists()

    # Saved like checks, with MACs learned on the way; no alerts for routers not in use.
    store = parts["store"]
    assert [c.router_id for c in store.recent_checks(10)] == ["cafe", "nb"]
    assert [r.id for r in result.linked] == ["nb", "cafe"]
    assert set(engine.settings.router("nb").macs) == {mac(NB_LAN), mac(NB_BSSID)}
    assert parts["notifier"].alerts == []
    assert (
        store.events("nb", 5)[0].message == f"Tested during Test all: OK, score {round(nb.score)}"
    )
    assert store.last_event(TEST_ALL_EVENT).message == (
        "Test all: tested Neighbor, Cafe; back on “ZTE-Home”"
    )


def test_a_router_that_does_not_connect_is_reported_and_skipped(parts, tmp_path) -> None:
    routers = routers_with_cafe(parts)
    parts["switcher"].unreachable.add("Neighbor")
    runner, _, _ = runner_for(parts, routers, tmp_path)
    result = run(runner, plan_for(parts, routers))
    assert [(o.router.id, o.problem) for o in result.failed] == [("nb", NO_CONNECTION)]
    assert [o.router.id for o in result.tested] == ["cafe"]
    assert result.restored and not result.cancelled
    assert "couldn't connect to Neighbor" in parts["store"].last_event(TEST_ALL_EVENT).message


def test_windows_refusing_to_connect_is_reported(parts, tmp_path) -> None:
    routers = sample_routers()

    def refuse(_name):
        raise WifiUnavailableError("WlanConnect failed with Windows error 87")

    parts["switcher"].on_connect = refuse
    runner, _, _ = runner_for(parts, routers, tmp_path)
    result = run(runner, plan_for(parts, routers))
    assert result.failed[0].problem == (
        "Windows couldn't connect: WlanConnect failed with Windows error 87"
    )
    assert result.restored  # never left the ZTE, so nothing to restore


def test_cancel_stops_saves_nothing_half_done_and_goes_back(parts, tmp_path) -> None:
    routers = routers_with_cafe(parts)
    runner, _, marker = runner_for(parts, routers, tmp_path)
    cancel = threading.Event()

    def cancel_while_testing(progress):
        if progress.stage is Stage.TESTING:
            cancel.set()  # the pings stop early and the test is dropped

    result = run(runner, plan_for(parts, routers), cancel=cancel, progress=cancel_while_testing)
    assert result.cancelled and result.outcomes == ()
    assert result.restored
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert parts["store"].recent_checks(10) == []
    assert not marker.path.exists()


def test_exit_asks_windows_to_go_back_and_keeps_the_marker(parts, tmp_path) -> None:
    routers = sample_routers()
    runner, _, marker = runner_for(parts, routers, tmp_path, connect_timeout_s=5.0)
    cancel, exiting = threading.Event(), threading.Event()

    def exit_while_testing(progress):
        if progress.stage is Stage.TESTING:
            cancel.set()
            exiting.set()

    result = run(runner, plan_for(parts, routers), cancel, exiting, exit_while_testing)
    assert result.exited and not result.restored
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]  # not awaited
    assert marker.load().ssid == "ZTE-Home"  # for the next start
    assert "asked Windows to reconnect" in parts["store"].last_event(TEST_ALL_EVENT).message


def test_restoring_is_tried_twice(parts, tmp_path) -> None:
    routers = sample_routers()
    parts["switcher"].unreachable.add("ZTE-Home")
    runner, _, marker = runner_for(parts, routers, tmp_path)
    result = run(runner, plan_for(parts, routers))
    assert not result.restored and not result.exited
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home", "connect ZTE-Home"]
    assert not marker.path.exists()  # you were told; don't switch you back later
    assert (
        parts["store"]
        .last_event(TEST_ALL_EVENT)
        .message.endswith("couldn't reconnect to “ZTE-Home”")
    )


def test_off_wifi_before_means_off_wifi_after(parts, tmp_path) -> None:
    parts["wifi"].connection = None
    parts["netinfo"].gateway = None
    routers = sample_routers()
    runner, _, _ = runner_for(parts, routers, tmp_path)
    plan = plan_for(parts, routers)
    assert plan.origin is None and plan.current is None
    result = run(runner, plan, scheduled=True)
    assert result.restored and parts["wifi"].connection is None
    assert parts["switcher"].calls == ["connect Neighbor", "disconnect"]
    assert parts["store"].last_event(TEST_ALL_EVENT).message == (
        "Scheduled test all: tested Neighbor; Wi-Fi disconnected again"
    )


def test_errors_still_restore_the_connection(parts, tmp_path) -> None:
    routers = sample_routers()
    runner, _, _ = runner_for(parts, routers, tmp_path)

    class BrokenPing:
        def ping(self, *args, **kwargs):
            raise OSError("IcmpCreateFile failed")

    runner._engine._ping = BrokenPing()
    with pytest.raises(OSError, match="IcmpCreateFile"):
        run(runner, plan_for(parts, routers))
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert parts["wifi"].connection.ssid == "ZTE-Home"


def test_a_plan_that_cannot_run_is_refused(parts, tmp_path) -> None:
    runner, _, _ = runner_for(parts, sample_routers(), tmp_path)
    with pytest.raises(ValueError, match="nothing to test"):
        run(runner, plan_test_all((), None, None, [], []))


# --- the Switch button ---------------------------------------------------------------------


def test_switch_connects_and_logs(parts, tmp_path) -> None:
    routers = sample_routers()
    runner, _, _ = runner_for(parts, routers, tmp_path)
    result = runner.switch(plan_for(parts, routers), routers[1], stop=threading.Event())
    assert result.ok and parts["wifi"].connection.ssid == "Neighbor"
    assert parts["store"].events("nb", 1)[0].message == "Switched to Neighbor"


def test_a_failed_switch_goes_back(parts, tmp_path) -> None:
    routers = sample_routers()
    parts["switcher"].unreachable.add("Neighbor")
    runner, _, _ = runner_for(parts, routers, tmp_path)
    result = runner.switch(plan_for(parts, routers), routers[1], stop=threading.Event())
    assert not result.ok and result.restored and result.problem == NO_CONNECTION
    assert parts["wifi"].connection.ssid == "ZTE-Home"
    assert parts["store"].events("nb", 1)[0].message == (
        f"Couldn't switch to Neighbor: {NO_CONNECTION}"
    )


def test_switch_explains_why_it_cannot(parts, tmp_path) -> None:
    zte, nb, gone = sample_routers()
    runner, _, _ = runner_for(parts, (zte, nb, gone), tmp_path)
    plan = plan_for(parts, (zte, nb, gone))
    stop = threading.Event()
    assert runner.switch(plan, gone, stop=stop).problem == "Not in range right now."
    assert runner.switch(plan, zte, stop=stop).problem == "You're already on ZTE."
    assert parts["switcher"].calls == []
    denied = TestAllPlan(None, None, blocker=LOCATION_BLOCKER)
    assert runner.switch(denied, nb, stop=stop).problem == LOCATION_BLOCKER


# --- after an interrupted run -------------------------------------------------------------


def test_marker_file_round_trip(tmp_path) -> None:
    marker = MarkerFile(tmp_path / "sub" / "marker.json")
    assert marker.load() is None
    saved = RestoreMarker(T0, "ZTE-Home", "ZTE-Home", ("Neighbor", "Cafe"))
    marker.save(saved)
    assert marker.load() == saved
    marker.path.write_text("{not json", encoding="utf-8")
    assert marker.load() is None
    marker.clear()
    marker.clear()  # already gone: fine
    assert not marker.path.exists()


def test_recovery_rules() -> None:
    marker = RestoreMarker(T0, "ZTE-Home", "ZTE-Home", ("Neighbor",))
    on = lambda ssid: WifiConnection(ssid, None, 80, ssid)  # noqa: E731
    soon = T0 + timedelta(minutes=5)
    assert recovery_for(marker, on("Neighbor"), soon) is Recovery.RECONNECT
    assert recovery_for(marker, on("ZTE-Home"), soon) is Recovery.NONE  # already back
    assert recovery_for(marker, on("Hotel"), soon) is Recovery.NONE  # you moved on
    assert recovery_for(marker, None, soon) is Recovery.NONE
    assert recovery_for(marker, on("Neighbor"), T0 + timedelta(hours=3)) is Recovery.NONE
    off = RestoreMarker(T0, None, None, ("Neighbor",))
    assert recovery_for(off, on("Neighbor"), soon) is Recovery.DISCONNECT


def test_recover_goes_back_after_a_crash(parts, tmp_path) -> None:
    routers = sample_routers()
    runner, _, marker = runner_for(parts, routers, tmp_path)
    parts["switcher"].connect("Neighbor")  # the app crashed while on the Neighbor
    marker.save(RestoreMarker(T0, "ZTE-Home", "ZTE-Home", ("Neighbor",)))
    event = runner.recover(stop=threading.Event())
    assert event.message == "Went back to “ZTE-Home” after an interrupted Test all"
    assert parts["wifi"].connection.ssid == "ZTE-Home"
    assert not marker.path.exists()
    assert runner.recover(stop=threading.Event()) is None  # nothing left to do


def test_recover_leaves_you_alone_when_unsure(parts, tmp_path) -> None:
    runner, _, marker = runner_for(parts, sample_routers(), tmp_path)
    marker.save(RestoreMarker(T0, "Office", "Office", ("Neighbor",)))
    assert runner.recover(stop=threading.Event()) is None  # on the ZTE: not a tested one
    assert not marker.path.exists()
    assert parts["switcher"].calls == []
