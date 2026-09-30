"""AppController: checks run off the UI thread and report back through signals."""

import codecs
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, timedelta

import pytest

from fakes import (
    GW,
    T0,
    ZTE_BSSID,
    ZTE_LAN,
    FakeIdle,
    FakeStartup,
    FakeWatcher,
    cable_gateway,
    gateway_info,
    network_parts,
    sample_routers,
)
from router_checker.core.models import AlertKind, Event, LinkKind, Router, Verdict
from router_checker.core.presentation import StatusLevel
from router_checker.core.quiet_hours import QuietHours, parse_hhmm
from router_checker.core.settings import Settings, load_settings
from router_checker.core.switching import ON_ETHERNET, MarkerFile, RestoreMarker, SwitchTiming
from router_checker.ui.controller import MARKER_FILE, AppController, Services, gateway_key

FAST = SwitchTiming(connect_timeout_s=1.0, settle_s=0.0, poll_s=0.01)

TIMEOUT = 5000


@pytest.fixture
def make(tmp_path):
    made = []

    def factory(settings=None, **overrides):
        parts = network_parts()
        parts.update(overrides)
        services = Services(
            wifi=parts["wifi"],
            ping=parts["ping"],
            dns=parts["dns"],
            netinfo=parts["netinfo"],
            clock=parts["clock"],
            watcher=FakeWatcher(),
            switcher=parts["switcher"],
            idle=parts.get("idle", FakeIdle()),
            startup=parts.get("startup"),
        )
        settings = settings or Settings(pings_per_target=4, routers=sample_routers())
        controller = AppController(
            settings, tmp_path / "settings.json", parts["store"], services, tz=UTC,
            switch_timing=FAST,
        )  # fmt: skip
        made.append(controller)
        return controller, services, parts

    yield factory
    for controller in made:
        controller.shutdown()


class GatedPing:
    """Blocks every ping until released (or stopped), so a check stays 'running'."""

    def __init__(self, inner):
        self.inner = inner
        self.gate = threading.Event()
        self.threads = set()

    def ping(self, *args, stop=None, source=None):
        self.threads.add(threading.current_thread())
        deadline = time.monotonic() + TIMEOUT / 1000
        while not self.gate.wait(0.01):
            if stop is not None and stop.is_set():
                return []
            assert time.monotonic() < deadline
        return self.inner.ping(*args, stop=stop)


def test_check_runs_in_worker_and_reports(qtbot, make) -> None:
    parts = network_parts()
    gated = GatedPing(parts["ping"])
    gated.gate.set()
    controller, _, _ = make(ping=gated)
    with (
        qtbot.waitSignal(controller.checkStarted, timeout=TIMEOUT),
        qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT) as finished,
    ):
        controller.check_now()
    snapshot = finished.args[0]
    assert snapshot.status.level is StatusLevel.GOOD
    assert snapshot.report.match.router.id == "zte"
    assert threading.main_thread() not in gated.threads
    assert controller.next_check_at == snapshot.report.timestamp + timedelta(minutes=5)
    assert set(snapshot.sparklines) == {"zte", "nb", "gone"}
    assert len(snapshot.sparklines["zte"]) == 1


def test_settings_changed_during_a_check_apply_after_it(qtbot, make, tmp_path) -> None:
    parts = network_parts()
    gated = GatedPing(parts["ping"])
    controller, _, _ = make(ping=gated)
    controller.check_now()
    assert controller.is_checking
    with qtbot.waitSignal(controller.settingsChanged, timeout=TIMEOUT):
        controller.update_settings(replace(controller.settings, pings_per_target=6))
    assert controller._engine.settings.pings_per_target == 4  # not while running
    assert load_settings(tmp_path / "settings.json").pings_per_target == 6  # saved at once
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        gated.gate.set()
    assert controller._engine.settings.pings_per_target == 6


def test_check_now_while_running_runs_once_more(qtbot, make) -> None:
    parts = network_parts()
    gated = GatedPing(parts["ping"])
    controller, _, _ = make(ping=gated)
    finished = []
    controller.checkFinished.connect(finished.append)
    controller.check_now()
    controller.check_now()
    controller.check_now()
    gated.gate.set()
    qtbot.waitUntil(lambda: len(finished) == 2, timeout=TIMEOUT)
    qtbot.wait(200)
    assert len(finished) == 2


def test_linked_macs_are_saved(qtbot, make, tmp_path) -> None:
    home = Router("home", "Home", "#0078D4", ssid="ZTE-Home")
    controller, _, _ = make(Settings(pings_per_target=4, routers=(home,)))
    with qtbot.waitSignal(controller.settingsChanged, timeout=TIMEOUT) as changed:
        controller.check_now()
    macs = {str(m) for m in changed.args[0].router("home").macs}
    assert macs == {ZTE_LAN, ZTE_BSSID}
    saved = json.loads((tmp_path / "settings.json").read_text())
    assert set(saved["routers"][0]["macs"]) == {ZTE_LAN, ZTE_BSSID}


def test_failed_check_is_reported_and_rescheduled(qtbot, make) -> None:
    class BrokenNetInfo:
        def gateway(self, choice=None):
            raise OSError("adapter vanished")

    controller, _, _ = make(netinfo=BrokenNetInfo())
    with qtbot.waitSignal(controller.checkFailed, timeout=TIMEOUT) as failed:
        controller.check_now()
    assert "adapter vanished" in failed.args[0]
    assert controller.last_failure == failed.args[0]
    assert controller.next_check_at is not None
    assert controller._timer.isActive()


def test_alerts_respect_the_notification_setting(qtbot, make) -> None:
    settings = Settings(pings_per_target=4, routers=sample_routers(), unstable_checks=1)
    controller, _, parts = make(settings)
    parts["ping"].replies[GW] = [2.0, None]
    with qtbot.waitSignal(controller.alertRaised, timeout=TIMEOUT) as raised:
        controller.check_now()
    assert raised.args[0].kind is AlertKind.UNSTABLE

    quiet = replace(settings, notifications_enabled=False)
    controller2, _, parts2 = make(quiet)
    parts2["ping"].replies[GW] = [2.0, None]
    with (
        qtbot.assertNotEmitted(controller2.alertRaised, wait=300),
        qtbot.waitSignal(controller2.checkFinished, timeout=TIMEOUT),
    ):
        controller2.check_now()


def test_network_change_triggers_a_check(qtbot, make) -> None:
    controller, services, parts = make()
    controller.network_settle_ms = 10
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.start()
    assert services.watcher.started

    # Same gateway: a route change alone does not trigger a check.
    services.watcher.changed = True
    with qtbot.assertNotEmitted(controller.checkStarted, wait=1500):
        pass

    parts["netinfo"].wifi = gateway_info(GW, "11-22-33-44-55-66")
    services.watcher.changed = True
    with qtbot.waitSignal(controller.checkStarted, timeout=3000):
        pass


def test_network_change_switch_stops_the_watcher(qtbot, make) -> None:
    controller, services, _ = make()
    controller.start()
    assert services.watcher.started
    controller.update_settings(replace(controller.settings, check_on_network_change=False))
    assert not services.watcher.started
    controller.update_settings(replace(controller.settings, check_on_network_change=True))
    assert services.watcher.started


def test_router_edits(qtbot, make) -> None:
    controller, _, _ = make()
    new = Router.create("Cafe", ssid="Cafe")
    with qtbot.waitSignal(controller.checkStarted, timeout=TIMEOUT):
        controller.add_router(new)
    assert controller.settings.router(new.id) == new
    qtbot.waitUntil(lambda: not controller.is_checking, timeout=TIMEOUT)
    with qtbot.assertNotEmitted(controller.checkStarted, wait=200):
        controller.update_router(replace(new, name="Coffee", color="#107C10"))  # cosmetic
    controller.remove_router(new.id)
    assert controller.settings.router(new.id) is None
    controller.complete_first_run(15)
    assert controller.settings.first_run_done and controller.settings.interval_min == 15


def test_location_probe(qtbot, make) -> None:
    controller, services, _ = make()
    with qtbot.waitSignal(controller.locationStatus, timeout=TIMEOUT) as status:
        controller.probe_location()
    assert status.args == [True]
    services.wifi.denied = True
    with qtbot.waitSignal(controller.locationStatus, timeout=TIMEOUT) as status:
        controller.probe_location()
    assert status.args == [False]


def test_background_reads(qtbot, make) -> None:
    controller, _, _ = make()
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()
    results = []
    controller.load_history(None, results.append)
    controller.load_router_details("zte", results.append)
    controller.scan_networks(results.append, results.append)
    qtbot.waitUntil(lambda: len(results) == 3, timeout=TIMEOUT)
    history = next(r for r in results if hasattr(r, "checks"))
    details = next(r for r in results if hasattr(r, "last_seen"))
    scan = next(r for r in results if hasattr(r, "entries"))
    assert len(history.checks) == 1
    assert details.last_check is not None and details.last_seen is not None
    assert len(scan.entries) == 3


def test_shutdown_ends_a_running_check_quickly(qtbot, make) -> None:
    gated = GatedPing(network_parts()["ping"])  # never released
    controller, _, parts = make(ping=gated)
    finished = []
    controller.checkFinished.connect(finished.append)
    controller.check_now()
    qtbot.waitUntil(lambda: bool(gated.threads), timeout=TIMEOUT)  # the pings are running
    started = time.monotonic()
    controller.shutdown()
    assert time.monotonic() - started < 1.0
    assert controller._pool.activeThreadCount() == 0
    qtbot.wait(100)
    assert not finished
    assert parts["store"].recent_checks(10) == []


def test_quiet_hours_hold_back_alerts(qtbot, make) -> None:
    # The fake clock says 12:00 UTC.
    quiet = QuietHours(True, parse_hhmm("11:00"), parse_hhmm("13:00"))
    settings = Settings(
        pings_per_target=4, routers=sample_routers(), unstable_checks=1, quiet_hours=quiet
    )
    controller, _, parts = make(settings)
    parts["ping"].replies[GW] = [2.0, None]
    with (
        qtbot.assertNotEmitted(controller.alertRaised, wait=300),
        qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT) as finished,
    ):
        controller.check_now()
    assert finished.args[0].report.alert is not None  # still decided and logged
    assert parts["store"].events("zte", 5)
    assert not controller.alerts_allowed(finished.args[0].report.timestamp)
    parts["clock"].advance(90)  # 13:30: quiet hours are over
    assert controller.alerts_allowed(parts["clock"].now())


def test_export_checks_writes_csv_with_bom(qtbot, make, tmp_path) -> None:
    controller, _, _ = make()
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()
    target = tmp_path / "out.csv"
    results = []
    controller.export_checks(target, results.append, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=TIMEOUT)
    assert results == [1]
    data = target.read_bytes()
    assert data.startswith(codecs.BOM_UTF8 + b"Time,UTC offset,Router,")
    assert "2026-09-29 12:00:00,+00:00,ZTE," in data.decode("utf-8-sig")


def test_export_errors_are_reported(qtbot, make, tmp_path) -> None:
    controller, _, _ = make()
    results = []
    controller.export_checks(tmp_path / "missing" / "out.csv", results.append, results.append)
    qtbot.waitUntil(lambda: bool(results), timeout=TIMEOUT)
    assert isinstance(results[0], OSError)


# --- Test all and switching ---------------------------------------------------------------


def plan_now(qtbot, controller):
    plans = []
    controller.plan_test_all(plans.append)
    qtbot.waitUntil(lambda: bool(plans), timeout=TIMEOUT)
    return plans[0]


def gated_parts():
    """The fake network with every ping held until the gate opens (or the ping is stopped)."""
    parts = network_parts()
    parts["ping"] = GatedPing(parts["ping"])
    return parts


def test_test_all_runs_and_ends_with_a_check(qtbot, make) -> None:
    controller, _, parts = make()
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()
    assert controller.last_snapshot.switchable == {"nb"}
    plan = plan_now(qtbot, controller)
    assert [c.router.id for c in plan.to_test] == ["nb"]
    seen = []
    controller.activityChanged.connect(lambda: seen.append(controller.activity))
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT) as finished:
        assert controller.start_test_all(plan)
        assert controller.is_busy and controller.is_testing_all
        assert controller.next_check_at is None  # the timer pauses
        assert not controller.can_switch_to("nb")
    message, scheduled = finished.args
    assert (message.title, scheduled) == ("Test all finished", False)
    assert message.text.startswith("Tested Neighbor and ZTE. Best: ")
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert "Connecting to Neighbor (1 of 1)…" in seen
    assert "Checking the network you're on…" in seen and seen[-1] is None
    assert not controller.is_busy and controller.next_check_at is not None
    report = controller.last_snapshot.report
    assert report.match.router.id == "zte"
    nb = next(st for st in report.statuses if st.router.id == "nb")
    assert not nb.score.estimated  # measured during Test all
    macs = {str(m) for m in controller.settings.router("nb").macs}
    assert macs == {"22-22-22-22-22-20", "22-22-22-22-22-22"}  # learned on the way, saved


def test_checks_pause_while_test_all_runs(qtbot, make) -> None:
    parts = gated_parts()
    controller, services, _ = make(**parts)
    assert controller.start_test_all(plan_now(qtbot, controller))
    qtbot.waitUntil(lambda: controller.activity == "Testing Neighbor (1 of 1)…", timeout=TIMEOUT)
    qtbot.waitUntil(lambda: bool(parts["ping"].threads), timeout=TIMEOUT)
    with qtbot.assertNotEmitted(controller.checkStarted, wait=300):
        controller.check_now()
        services.watcher.changed = True
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        parts["ping"].gate.set()


def test_cancel_goes_back_and_keeps_nothing_half_done(qtbot, make) -> None:
    parts = gated_parts()
    controller, _, _ = make(**parts)
    assert controller.start_test_all(plan_now(qtbot, controller))
    qtbot.waitUntil(lambda: bool(parts["ping"].threads), timeout=TIMEOUT)
    assert controller.run_state.can_cancel
    controller.cancel_test_all()
    assert controller.activity == "Cancelling…" and not controller.run_state.can_cancel
    qtbot.waitUntil(
        lambda: controller.activity == "Checking the network you're on…", timeout=TIMEOUT
    )
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT) as finished:
        parts["ping"].gate.set()  # lets the closing check run
    assert finished.args[0].title == "Test all cancelled"
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert parts["store"].recent_checks(10, "nb") == []


def test_exit_during_test_all_asks_windows_to_go_back(qtbot, make, tmp_path) -> None:
    parts = gated_parts()  # never released
    controller, _, _ = make(**parts)
    assert controller.start_test_all(plan_now(qtbot, controller))
    qtbot.waitUntil(lambda: bool(parts["ping"].threads), timeout=TIMEOUT)
    started = time.monotonic()
    controller.shutdown()
    assert time.monotonic() - started < 1.0
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert MarkerFile(tmp_path / MARKER_FILE).load().ssid == "ZTE-Home"  # for the next start


def test_test_all_waits_for_a_running_check(qtbot, make) -> None:
    parts = gated_parts()
    controller, _, _ = make(**parts)
    plan = plan_now(qtbot, controller)
    controller.check_now()
    assert controller.start_test_all(plan)
    assert controller.activity == "Test all starts after this check…"
    assert not controller.start_test_all(plan)  # one at a time
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        parts["ping"].gate.set()
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]


def test_switch_to_connects_and_checks_the_new_router(qtbot, make) -> None:
    controller, _, parts = make()
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()
    assert controller.can_switch_to("nb") and not controller.can_switch_to("gone")
    with qtbot.waitSignal(controller.switchFinished, timeout=TIMEOUT) as finished:
        assert controller.switch_to("nb")
        assert controller.activity == "Switching to Neighbor…"
    assert finished.args[0].title == "Switched to Neighbor"
    report = controller.last_snapshot.report
    assert report.connection.ssid == "Neighbor" and report.match.router.id == "nb"

    # The ZTE has no Wi-Fi name, but the first check learned its Wi-Fi MAC: that's enough.
    with qtbot.waitSignal(controller.switchFinished, timeout=TIMEOUT) as finished:
        assert controller.switch_to("zte")
    assert finished.args[0].title == "Switched to ZTE"
    with qtbot.waitSignal(controller.switchFinished, timeout=TIMEOUT) as finished:
        assert controller.switch_to("gone")
    assert (finished.args[0].title, finished.args[0].text) == (
        "Can't switch to Gone",
        "Not in range right now.",
    )
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]


def test_scheduled_test_all_waits_until_you_are_away(qtbot, make) -> None:
    parts = network_parts()
    parts["idle"] = idle = FakeIdle(60)
    settings = Settings(pings_per_target=4, routers=sample_routers(), scheduled_test_all=True)
    controller, _, _ = make(settings, **parts)
    controller.maybe_run_scheduled_test_all()
    qtbot.wait(100)
    assert not controller.is_busy and parts["switcher"].calls == []

    idle.seconds = 600
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT) as finished:
        controller.maybe_run_scheduled_test_all()
    assert finished.args[1] is True  # scheduled
    assert parts["store"].last_event("test_all").message.startswith("Scheduled test all: ")
    controller.maybe_run_scheduled_test_all()  # not due again for 2 hours
    qtbot.wait(100)
    assert not controller.is_busy
    parts["clock"].advance(120)
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        controller.maybe_run_scheduled_test_all()


def test_the_last_scheduled_run_is_read_from_history(qtbot, make) -> None:
    parts = network_parts()
    parts["idle"] = FakeIdle(600)
    parts["store"].add_event(Event(T0 - timedelta(minutes=30), None, "test_all", "Test all: ..."))
    settings = Settings(pings_per_target=4, routers=sample_routers(), scheduled_test_all=True)
    controller, _, _ = make(settings, **parts)
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.start()
    qtbot.wait(100)  # the last run's time is read in the background too
    controller.maybe_run_scheduled_test_all()
    qtbot.wait(100)
    assert parts["switcher"].calls == []  # it ran 30 minutes ago; the interval is 2 hours


def test_start_goes_back_after_an_interrupted_test_all(qtbot, make, tmp_path) -> None:
    parts = network_parts()
    parts["switcher"].connect("Neighbor")  # the app crashed while on the Neighbor
    marker = RestoreMarker(T0, "ZTE-Home", "ZTE-Home", ("Neighbor",))
    MarkerFile(tmp_path / MARKER_FILE).save(marker)
    controller, _, _ = make(**parts)
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT) as finished:
        controller.start()
        assert controller.activity.startswith("Going back to your network")
    assert finished.args[0].report.match.router.id == "zte"
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    assert not (tmp_path / MARKER_FILE).exists()
    messages = [e.message for e in parts["store"].events(None, 5)]
    assert "Went back to “ZTE-Home” after an interrupted Test all" in messages


# --- automatic switching --------------------------------------------------------------------


class NetworkPing:
    """Internet pings behave like the network you're on: bad on the ZTE, good elsewhere,
    and nothing answers on a network in ``broken``."""

    def __init__(self, parts):
        self.inner, self.wifi, self.broken = parts["ping"], parts["wifi"], set()

    def ping(self, address, count, timeout_ms, spacing_ms, stop=None, source=None):
        ssid = self.wifi.connection.ssid if self.wifi.connection else None
        if ssid in self.broken:
            return [None] * count
        if ssid == "ZTE-Home" and address != GW:
            return [150.0, None, 190.0, 150.0][:count]
        return self.inner.ping(address, count, timeout_ms, spacing_ms, stop=stop)


def auto_parts(**settings):
    parts = network_parts()
    parts["ping"] = NetworkPing(parts)
    return parts, Settings(pings_per_target=4, routers=sample_routers(), **settings)


def test_the_app_switches_to_a_router_that_stays_better(qtbot, make) -> None:
    parts, settings = auto_parts(auto_switch=True)
    controller, _, _ = make(settings, **parts)
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        controller.start_test_all(plan_now(qtbot, controller))  # measures the Neighbor
    assert controller.last_snapshot.report.recommendation.router_id == "nb"  # check 1 of 3
    assert controller.auto_switch_text == "Neighbor has been better for 1 of 3 checks."
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()
    with qtbot.waitSignal(controller.autoSwitched, timeout=TIMEOUT) as switched:
        controller.check_now()  # the third: switch
    message = switched.args[0]
    assert (message.title, message.back_to) == ("Switched to Neighbor", "zte")
    assert controller.last_snapshot.report.connection.ssid == "Neighbor"
    assert controller.auto_switch_text.startswith("Automatic switching pauses for 30 more min")
    assert (
        parts["store"]
        .events("nb", 1)[0]
        .message.startswith(
            "Switched automatically to Neighbor: Neighbor was clearly better for 3 checks in a row"
        )
    )


def test_it_goes_back_when_the_new_router_fails_right_away(qtbot, make) -> None:
    parts, settings = auto_parts(auto_switch=True)
    connects = []

    def break_neighbor_on_the_second_visit(name):
        connects.append(name)
        if connects.count("Neighbor") == 2:  # Test all went fine; the switch doesn't
            parts["ping"].broken.add("Neighbor")

    parts["switcher"].on_connect = break_neighbor_on_the_second_visit
    controller, _, _ = make(settings, **parts)
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        controller.start_test_all(plan_now(qtbot, controller))
    messages = []
    controller.autoSwitched.connect(messages.append)
    for _ in range(2):
        with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
            controller.check_now()
    qtbot.waitUntil(lambda: len(messages) == 2 and not controller.is_busy, timeout=TIMEOUT)
    assert [m.title for m in messages] == ["Switched to Neighbor", "Back on ZTE"]
    assert parts["switcher"].calls == [
        "connect Neighbor", "connect ZTE-Home", "connect Neighbor", "connect ZTE-Home"
    ]  # fmt: skip
    assert controller.last_snapshot.report.connection.ssid == "ZTE-Home"


def test_nothing_switches_while_it_is_off(qtbot, make) -> None:
    parts, settings = auto_parts()  # off by default
    controller, _, _ = make(settings, **parts)
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        controller.start_test_all(plan_now(qtbot, controller))
    with qtbot.assertNotEmitted(controller.autoSwitched, wait=300):
        for _ in range(4):
            with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
                controller.check_now()
    assert controller.auto_switch_text is None
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]  # Test all only


# --- start with Windows ---------------------------------------------------------------------


def test_start_with_windows_goes_through_windows(make) -> None:
    controller, _, _ = make()
    assert not controller.can_start_with_windows  # no startup service: the card is hidden
    assert not controller.starts_with_windows()
    startup = FakeStartup()
    controller, _, _ = make(startup=startup)
    assert controller.can_start_with_windows
    assert controller.set_start_with_windows(True) is None
    assert startup.on and controller.starts_with_windows()
    startup.fail = True
    assert controller.set_start_with_windows(False) == (
        "Windows didn't allow the change (Access is denied)."
    )
    assert controller.starts_with_windows()


# --- Ethernet ---------------------------------------------------------------------------


def test_on_a_cable_test_all_and_switching_are_off(qtbot, make) -> None:
    parts, settings = auto_parts(auto_switch=True)
    controller, _, _ = make(settings, **parts)
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        controller.start_test_all(plan_now(qtbot, controller))  # the Neighbor measured better
    assert controller.last_snapshot.report.recommendation is not None
    parts["netinfo"].cable = cable_gateway()  # now plugged into the ZTE
    with qtbot.assertNotEmitted(controller.autoSwitched, wait=300):
        for _ in range(3):
            with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
                controller.check_now()
    assert controller.on_ethernet
    report = controller.last_snapshot.report
    assert report.recommendation is None  # no advice to switch Wi-Fi
    assert report.match.router.id == "zte" and report.record.link is LinkKind.ETHERNET
    assert not controller.can_switch_to("nb")
    assert not controller.switch_to("nb")
    assert plan_now(qtbot, controller).blocker == ON_ETHERNET
    assert controller.auto_switch_text == "Automatic switching is paused while you're on Ethernet."
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]  # Test all only


def test_even_a_down_router_isnt_switched_away_from_on_a_cable(qtbot, make) -> None:
    parts, settings = auto_parts(auto_switch=True)
    parts["netinfo"].cable = cable_gateway()
    parts["ping"].broken.add("ZTE-Home")  # everything times out
    controller, _, _ = make(settings, **parts)
    with qtbot.assertNotEmitted(controller.autoSwitched, wait=300):
        for _ in range(3):
            with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
                controller.check_now()
    assert controller.last_snapshot.report.record.verdict is Verdict.ROUTER_UNREACHABLE
    assert parts["switcher"].calls == []


def test_plugging_in_a_cable_counts_as_a_network_change() -> None:
    assert gateway_key(gateway_info(GW, ZTE_LAN)) != gateway_key(cable_gateway(GW, ZTE_LAN))
