"""Build the real windows with the fake network and render them in both themes."""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from qfluentwidgets import PushButton, Theme, setTheme

from fakes import GW, FakeWatcher, gateway_info, network_parts, sample_routers
from router_checker.core.models import Router, WifiConnection
from router_checker.core.presentation import StatusLevel
from router_checker.core.quiet_hours import QuietHours
from router_checker.core.settings import Settings
from router_checker.ui.controller import AppController, Services
from router_checker.ui.dialogs.first_run import FirstRunDialog
from router_checker.ui.dialogs.router_dialog import RouterDialog
from router_checker.ui.main_window import MainWindow
from router_checker.ui.style import app_icon, status_icon
from router_checker.ui.tray import TrayFlyoutView, TrayIcon

TIMEOUT = 5000


@pytest.fixture
def app_parts(qtbot, tmp_path):
    parts = network_parts()
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"], FakeWatcher()
    )
    settings = Settings(pings_per_target=4, routers=sample_routers(), first_run_done=True)
    controller = AppController(settings, tmp_path / "settings.json", parts["store"], services)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    yield controller, window, parts
    window.prepare_quit()
    controller.shutdown()
    setTheme(Theme.LIGHT)


def check(qtbot, controller) -> None:
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        controller.check_now()


def render_all(window) -> None:
    for page in (window.dashboard, window.routers, window.history, window.settings_page):
        window.switchTo(page)
        assert not page.grab().isNull()


def test_pages_render_in_both_themes(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    check(qtbot, controller)
    assert window.dashboard.current.name.text() == "ZTE"
    assert window.dashboard.current.status.title.text() == "Stable"
    assert set(window.dashboard._cards) == {"nb", "gone"}
    render_all(window)
    setTheme(Theme.DARK)
    render_all(window)


def test_router_details_and_history(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    check(qtbot, controller)
    window.show_router("zte")
    qtbot.waitUntil(lambda: bool(window.routers.details.last_check._rows), timeout=TIMEOUT)
    assert window.routers.currentWidget() is window.routers.details
    window.switchTo(window.history)
    window.history.reload()
    qtbot.waitUntil(lambda: window.history.checks.rowCount() == 1, timeout=TIMEOUT)
    assert window.history.checks.item(0, 1).text() == "ZTE"


def test_unknown_network_offers_to_add_it(qtbot, tmp_path) -> None:
    parts = network_parts()
    parts["wifi"].connection = WifiConnection("Cafe", None, 70)
    parts["netinfo"].gateway = gateway_info(GW, "99-99-99-99-99-99")
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"], FakeWatcher()
    )
    controller = AppController(Settings(pings_per_target=4), None, parts["store"], services)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    try:
        check(qtbot, controller)
        card = window.dashboard.current
        assert card.status.title.text() == "Unknown network"
        assert not card.add_button.isHidden()
        window.dashboard.addRouterRequested.disconnect(window.add_router)  # no modal dialog
        with qtbot.waitSignal(window.dashboard.addRouterRequested) as requested:
            card.add_button.click()
        prefill: Router = requested.args[0]
        assert prefill.ssid == "Cafe"
        assert [str(m) for m in prefill.macs] == ["99-99-99-99-99-99"]
    finally:
        window.prepare_quit()
        controller.shutdown()


def test_router_dialog_validates_and_builds_router(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    dialog = RouterDialog(controller, parent=window)
    qtbot.waitUntil(lambda: dialog.network_list.count() == 3, timeout=TIMEOUT)
    assert not dialog.validate()  # no name
    dialog.name.setText("Cafe")
    assert not dialog.validate()  # no MAC or SSID
    dialog.mac_edit.setText("b00ad59a7bb4")  # belongs to the ZTE
    assert not dialog.validate()
    assert "already belongs to ZTE" in dialog.error.text()
    dialog.mac_edit.setText("not a mac")
    assert not dialog.validate()
    dialog.mac_edit.clear()
    dialog._macs.clear()
    cafe_row = next(i for i, n in enumerate(dialog._networks) if n.ssid == "Cafe")
    dialog._pick(dialog.network_list.item(cafe_row))
    assert dialog.ssid.text() == "Cafe"
    assert dialog.validate()
    router = dialog.router()
    assert router.name == "Cafe" and router.ssid == "Cafe" and len(router.macs) == 1
    dialog.reject()


def test_first_run_steps(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    dialog = FirstRunDialog(controller, lambda: None, window)
    with qtbot.waitSignal(controller.locationStatus, timeout=TIMEOUT):
        controller.probe_location()
    assert dialog.location_icon._level is StatusLevel.GOOD
    assert not dialog.validate() and dialog.stack.currentIndex() == 1
    assert not dialog.validate() and dialog.stack.currentIndex() == 2
    assert dialog.yesButton.text() == "Finish"
    dialog._back()
    assert dialog.stack.currentIndex() == 1
    dialog.validate()
    assert dialog.validate()  # last step closes
    assert dialog.interval_min() == 5
    dialog.reject()


def click_menu_item(menu, text: str) -> None:
    """Click a tray menu entry the way the mouse does: through the menu's item list."""
    view = menu.view
    for row in range(view.count()):
        action = view.item(row).data(Qt.ItemDataRole.UserRole)
        if isinstance(action, QAction) and action.text() == text:
            view.itemClicked.emit(view.item(row))
            return
    raise AssertionError(f"no menu item {text!r}")


def test_tray_menu_items_work(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    tray = TrayIcon(controller, window)
    with qtbot.waitSignal(tray.exitRequested, timeout=TIMEOUT):
        click_menu_item(tray.menu, "Exit")
    with qtbot.waitSignal(controller.checkFinished, timeout=TIMEOUT):
        click_menu_item(tray.menu, "Check now")


def test_exit_button_and_shortcut(qtbot, app_parts) -> None:
    _, window, _ = app_parts
    with qtbot.waitSignal(window.exitRequested, timeout=TIMEOUT):
        window.dashboard.exit_button.click()
    shortcut = next(s for s in window.findChildren(QShortcut) if s.key() == QKeySequence("Ctrl+Q"))
    with qtbot.waitSignal(window.exitRequested, timeout=TIMEOUT):
        shortcut.activated.emit()


def test_exiting_during_setup_shows_setup_again_next_time(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    controller.update_settings(replace(controller.settings, first_run_done=False))
    window.bring_to_front = lambda: None  # keep the window off the screen

    def exit_during_setup() -> None:
        window.prepare_quit()
        window.findChild(FirstRunDialog).reject()

    QTimer.singleShot(300, exit_during_setup)
    window.run_first_run()
    assert not controller.settings.first_run_done


def test_flyout_and_icons_render(qtbot, app_parts) -> None:
    controller, _, _ = app_parts
    check(qtbot, controller)
    view = TrayFlyoutView(controller, lambda: None)
    qtbot.addWidget(view)
    assert view.title.text() == "ZTE · Stable"
    assert not view.grab().isNull()
    for level in StatusLevel:
        assert not status_icon(level).isNull()
    assert not app_icon().isNull()


def test_quiet_hours_and_notification_settings(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    page = window.settings_page
    page.quiet_switch.setChecked(True)
    assert controller.settings.quiet_hours == QuietHours(True)
    assert page.quiet_card.card.contentLabel.text() == "No notifications from 22:00 to 07:00."
    page.quiet_start.setTime(QTime(23, 15))
    page._apply()
    assert controller.settings.quiet_hours.start.strftime("%H:%M") == "23:15"
    page.show_notification_status(False)
    assert page.windows_card.contentLabel.text().startswith("Off for Router Checker")
    with qtbot.waitSignal(page.testNotificationRequested, timeout=TIMEOUT):
        page.windows_card.findChildren(PushButton)[0].click()


def test_closing_the_window_hints_once(qtbot, app_parts) -> None:
    _, window, _ = app_parts
    with qtbot.waitSignal(window.hiddenToTray, timeout=TIMEOUT):
        window.close()
    with qtbot.assertNotEmitted(window.hiddenToTray, wait=100):
        window.close()


def test_router_details_charts_and_popular_times(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    check(qtbot, controller)
    parts["clock"].advance(5)
    check(qtbot, controller)
    details = window.routers.details
    card, popular = details.history_card, details.popular_card
    card.show_router("zte")
    popular.show_router("zte")
    qtbot.waitUntil(lambda: card.note.text() != "", timeout=TIMEOUT)
    assert card.note.text() == "2 tests in the last 24 hours."
    assert card.latency.readout.text() == (
        "Gateway: median 2.0 ms, highest 2.0 ms · Internet: median 10 ms, highest 10 ms"
    )
    assert "Internet 10 ms" in card.latency.readout_at(parts["clock"].now().timestamp())
    assert card.loss.readout.text().startswith("Gateway: median 0%")
    assert card.score.readout.text().startswith("Score: median ")
    with qtbot.waitSignal(controller._taskDone, timeout=TIMEOUT):
        card.span.setCurrentItem("7 d")
    qtbot.waitUntil(lambda: card.note.text() == "2 tests in the last 7 days.", timeout=TIMEOUT)

    qtbot.waitUntil(lambda: popular.summary.text() != "", timeout=TIMEOUT)
    assert popular.summary.text().startswith("Not enough data yet (1 hour so far)")
    qtbot.keyClick(popular.heatmap, Qt.Key.Key_Right)
    assert popular.heatmap.cell == (0, 1)
    assert popular.readout.text().startswith("Monday 01:00–02:00: ")
    for theme in (Theme.LIGHT, Theme.DARK):
        setTheme(theme)
        assert not card.grab().isNull() and not popular.grab().isNull()


def test_history_compares_router_scores(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    check(qtbot, controller)
    window.history.reload()  # the page reloads when shown; the test window stays hidden
    chart = window.history.comparison.chart
    qtbot.waitUntil(lambda: chart.readout.text() != "", timeout=TIMEOUT)
    text = chart.readout.text()
    assert text.startswith("Latest: ZTE ") and ", Neighbor ~" in text
    assert not window.history.comparison.grab().isNull()
