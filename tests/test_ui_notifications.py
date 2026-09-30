"""NotificationCenter: toasts with buttons, the tray fallback and button clicks."""

import threading
from dataclasses import dataclass, field, replace
from datetime import UTC

import pytest

from fakes import T0, FakeWatcher, network_parts, sample_routers
from router_checker.core.models import Alert, AlertKind, Score, Verdict
from router_checker.core.presentation import Message, StatusLevel
from router_checker.core.settings import Settings
from router_checker.ui.controller import AppController, Services
from router_checker.ui.notifications import ALERT_LIFETIME, CHECK, OPEN, NotificationCenter

TIMEOUT = 5000
UNSTABLE = Alert(
    AlertKind.UNSTABLE,
    "zte",
    "ZTE",
    T0,
    Verdict.UNSTABLE,
    recommended_name="Cafe",
    recommended_score=Score(85, False),
)


@dataclass
class FakeToaster:
    shown: list[dict] = field(default_factory=list)
    broken: bool = False
    allowed: bool | None = True

    def show(self, **toast) -> None:
        if self.broken:
            raise OSError("no notification platform")
        self.shown.append(toast)

    def enabled(self) -> bool | None:
        return self.allowed


@dataclass
class FakeTray:
    messages: list[tuple[str, str, StatusLevel]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    def show_message(self, title, text, level=StatusLevel.UNKNOWN, ms=6000) -> None:
        self.messages.append((title, text, level))

    def notify_alert(self, alert: Alert) -> None:
        self.alerts.append(alert)


@pytest.fixture
def center(qtbot):
    parts = network_parts()
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"],
        FakeWatcher(), parts["switcher"],
    )  # fmt: skip
    controller = AppController(
        Settings(pings_per_target=4, routers=sample_routers()), None, parts["store"], services,
        tz=UTC,
    )  # fmt: skip
    toaster, tray = FakeToaster(), FakeTray()
    yield NotificationCenter(controller, tray, toaster), toaster, tray
    controller.shutdown()


def test_unstable_alert_toast(center) -> None:
    center, toaster, tray = center
    center.controller.alertRaised.emit(UNSTABLE)
    toast = toaster.shown[0]
    assert toast["tag"] == "alert-zte"
    assert toast["title"] == "ZTE is unstable"
    assert toast["body"].endswith("Try Cafe instead (score 85).")
    assert toast["buttons"] == [("Open", OPEN), ("Check now", CHECK)]
    assert toast["expires_in"] == ALERT_LIFETIME
    assert not tray.alerts


def test_back_to_normal_replaces_the_alert_and_only_opens(center) -> None:
    center, toaster, _ = center
    center.show_alert(Alert(AlertKind.RECOVERED, "zte", "ZTE", T0, Verdict.OK))
    toast = toaster.shown[0]
    assert toast["tag"] == "alert-zte" and toast["buttons"] == [("Open", OPEN)]


def test_tray_is_the_fallback(center) -> None:
    center, toaster, tray = center
    toaster.broken = True
    center.show_alert(UNSTABLE)
    center.notify_hidden()
    assert tray.alerts == [UNSTABLE]
    assert tray.messages[0][0] == "Router Checker is still running"
    center.toaster = None
    center.send_test()
    assert tray.messages[1][:2] == (
        "Notifications work",
        "This is how Router Checker tells you that your router is unstable.",
    )


def test_clicks_arrive_on_the_ui_thread(qtbot, center) -> None:
    center, toaster, _ = center
    center.show_alert(UNSTABLE)
    on_action = toaster.shown[0]["on_action"]
    handled_on = []
    center.openRequested.connect(lambda: handled_on.append(threading.current_thread()))
    with (
        qtbot.waitSignal(center.controller.checkStarted, timeout=TIMEOUT),
        qtbot.waitSignal(center.openRequested, timeout=TIMEOUT),
    ):
        threading.Thread(target=on_action, args=(CHECK,)).start()  # like a WinRT thread
    assert handled_on == [threading.main_thread()]
    with qtbot.waitSignal(center.openRequested, timeout=TIMEOUT):
        threading.Thread(target=on_action, args=("alert-zte",)).start()  # the toast itself


def test_test_notification_reports_the_windows_setting(qtbot, center) -> None:
    center, toaster, _ = center
    toaster.allowed = False
    with qtbot.waitSignal(center.statusChanged, timeout=TIMEOUT) as status:
        center.send_test()
    assert toaster.shown[0]["tag"] == "test"
    assert status.args == [False]


def test_an_unstable_alert_offers_to_switch_to_the_better_router(qtbot, center) -> None:
    center, toaster, _ = center
    with qtbot.waitSignal(center.controller.checkFinished, timeout=TIMEOUT):
        center.controller.check_now()  # learns which routers it can switch to
    center.show_alert(replace(UNSTABLE, recommended_name="Neighbor", recommended_id="nb"))
    toast = toaster.shown[-1]
    assert toast["buttons"] == [
        ("Open", OPEN),
        ("Check now", CHECK),
        ("Switch to Neighbor", "switch:nb"),
    ]
    with (
        qtbot.waitSignal(center.switchRequested, timeout=TIMEOUT) as requested,
        qtbot.waitSignal(center.openRequested, timeout=TIMEOUT),
    ):
        threading.Thread(target=toast["on_action"], args=("switch:nb",)).start()
    assert requested.args == ["nb"]
    center.show_alert(replace(UNSTABLE, recommended_name="Gone", recommended_id="gone"))
    assert len(toaster.shown[-1]["buttons"]) == 2  # not in range: no Switch button


def test_only_a_failed_reconnect_is_a_notification(center) -> None:
    center, toaster, tray = center
    center.controller.testAllFinished.emit(
        Message(StatusLevel.GOOD, "Test all finished", "."), False
    )
    assert toaster.shown == []
    problem = Message(
        StatusLevel.BAD, "Couldn't reconnect to “ZTE-Home”", "Connect to it.", restore_failed=True
    )
    center.controller.testAllFinished.emit(problem, True)
    assert (toaster.shown[0]["tag"], toaster.shown[0]["title"]) == ("reconnect", problem.title)
    toaster.broken = True
    center.controller.switchFinished.emit(problem)
    assert tray.messages[-1] == (problem.title, problem.text, StatusLevel.BAD)


def test_an_automatic_switch_offers_to_go_back(qtbot, center) -> None:
    center, toaster, _ = center
    switched = Message(StatusLevel.GOOD, "Switched to Neighbor", "Why.", back_to="zte")
    center.controller.autoSwitched.emit(switched)
    toast = toaster.shown[-1]
    assert (toast["tag"], toast["title"]) == ("auto-switch", "Switched to Neighbor")
    assert toast["buttons"] == [("Open", OPEN), ("Go back", "switch:zte")]
    with qtbot.waitSignal(center.switchRequested, timeout=TIMEOUT) as requested:
        threading.Thread(target=toast["on_action"], args=("switch:zte",)).start()
    assert requested.args == ["zte"]
    center.controller.autoSwitched.emit(Message(StatusLevel.WARNING, "Back on ZTE", "Why."))
    assert toaster.shown[-1]["buttons"] == [("Open", OPEN)]
