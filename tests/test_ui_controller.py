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
    ZTE_BSSID,
    ZTE_LAN,
    FakeWatcher,
    gateway_info,
    network_parts,
    sample_routers,
)
from router_checker.core.models import AlertKind, Router
from router_checker.core.presentation import StatusLevel
from router_checker.core.quiet_hours import QuietHours, parse_hhmm
from router_checker.core.settings import Settings, load_settings
from router_checker.ui.controller import AppController, Services

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
        )
        settings = settings or Settings(pings_per_target=4, routers=sample_routers())
        controller = AppController(
            settings, tmp_path / "settings.json", parts["store"], services, tz=UTC
        )
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

    def ping(self, *args, stop=None):
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
        def wifi_gateway(self):
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

    parts["netinfo"].gateway = gateway_info(GW, "11-22-33-44-55-66")
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
