"""Build the real windows with the fake network and render them in both themes."""

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from qfluentwidgets import InfoBar, PushButton, Theme, setTheme

from fakes import (
    GW,
    MIDDLE,
    WIFI_IP,
    ZTE_ADDRESS,
    FakeNetInfo,
    FakeStartup,
    FakeWatcher,
    FakeWifi,
    cable_gateway,
    gateway_info,
    middle_parts,
    middle_routers,
    network_parts,
    sample_routers,
)
from router_checker.core.models import (
    LinkChoice,
    Recommendation,
    Router,
    Score,
    WifiConnection,
)
from router_checker.core.presentation import Status, StatusLevel, tray_tooltip
from router_checker.core.quiet_hours import QuietHours
from router_checker.core.settings import Settings
from router_checker.core.switching import ON_ETHERNET, SwitchTiming
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
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"],
        FakeWatcher(), parts["switcher"], startup=FakeStartup(),
    )  # fmt: skip
    parts["startup"] = services.startup
    settings = Settings(pings_per_target=4, routers=sample_routers(), first_run_done=True)
    controller = AppController(
        settings, tmp_path / "settings.json", parts["store"], services,
        switch_timing=SwitchTiming(1.0, 0.0, 0.01),
    )  # fmt: skip
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
    parts["netinfo"].wifi = gateway_info(GW, "99-99-99-99-99-99")
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


def info_bars(window) -> list[tuple[str, str]]:
    return [(bar.title, bar.content) for bar in window.findChildren(InfoBar)]


def test_test_all_from_the_dashboard(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    check(qtbot, controller)
    asked, cards = [], []
    window.bring_to_front = lambda: None  # keep the window off the screen
    window.ask = lambda title, text, yes: asked.append((title, yes)) or True
    card = window.dashboard.run_card
    controller.activityChanged.connect(
        lambda: cards.append((card.isHidden(), card.detail.text(), card.cancel_button.isEnabled()))
    )
    with qtbot.waitSignal(controller.testAllFinished, timeout=TIMEOUT):
        window.dashboard.test_all_button.click()
    assert asked == [("Test all routers?", "Test all")]
    assert (False, "Connecting to Neighbor (1 of 1)…", True) in cards
    assert (False, "Checking the network you're on…", False) in cards
    assert card.isHidden()
    assert parts["switcher"].calls == ["connect Neighbor", "connect ZTE-Home"]
    _title, text = next(bar for bar in info_bars(window) if bar[0] == "Test all finished")
    assert text.startswith("Tested Neighbor and ZTE.")
    assert not window.dashboard.run_card.grab().isNull()


def test_test_all_explains_why_nothing_can_be_tested(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    parts["switcher"].saved.clear()
    asked = []
    window.bring_to_front = lambda: None
    window.ask = lambda title, text, yes: asked.append((title, text, yes)) or True
    window.test_all()
    qtbot.waitUntil(lambda: bool(asked), timeout=TIMEOUT)
    title, text, yes = asked[0]
    assert (title, yes) == ("No other router to test", None)
    assert "• Neighbor: Windows hasn't saved it." in text
    assert not controller.is_busy and parts["switcher"].calls == []


def test_switch_to_the_recommended_router(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    check(qtbot, controller)
    snap = controller.last_snapshot
    rec = Recommendation("nb", Score(90, False), Score(70, False), 20)
    controller.last_snapshot = replace(snap, report=replace(snap.report, recommendation=rec))
    window.dashboard.refresh()
    button = window.dashboard.current.switch_button
    assert not button.isHidden() and button.text() == "Switch to Neighbor"
    with qtbot.waitSignal(controller.switchFinished, timeout=TIMEOUT):
        button.click()
    assert parts["wifi"].connection.ssid == "Neighbor"
    assert ("Switched to Neighbor", "Checking it now.") in info_bars(window)


def test_scheduled_test_all_setting(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    page = window.settings_page
    assert not page.test_all_switch.isChecked() and not page.test_all_interval.isEnabled()
    page.test_all_switch.setChecked(True)
    assert controller.settings.scheduled_test_all and page.test_all_interval.isEnabled()
    page.test_all_interval.setCurrentIndex(page.test_all_interval.findData(4))
    assert controller.settings.test_all_interval_h == 4


def test_test_all_from_the_tray_and_the_keyboard(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    started = []
    window.test_all = lambda: started.append(True)
    click_menu_item(TrayIcon(controller, window).menu, "Test all now")
    assert started == [True]
    keys = [s.key() for s in window.findChildren(QShortcut)]
    assert QKeySequence("Ctrl+T") in keys


def test_automatic_switching_setting_and_dashboard_line(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    page = window.settings_page
    assert not page.auto_switch.isChecked()
    page.auto_switch.setChecked(True)
    assert controller.settings.auto_switch
    controller._policy.switched("zte", "nb", controller.now(), automatic=False)
    window.dashboard._tick()
    line = window.dashboard.current.auto_text
    assert not line.isHidden()
    assert line.text() == "Automatic switching pauses for 30 more min after the last switch."
    page.auto_switch.setChecked(False)
    window.dashboard._tick()
    assert line.isHidden()


def test_the_start_with_windows_switch_shows_what_windows_has(qtbot, app_parts) -> None:
    _, window, parts = app_parts
    page, startup = window.settings_page, parts["startup"]
    assert not page.startup_card.isHidden()
    assert not page.startup.isChecked()
    page.startup.setChecked(True)
    assert startup.on
    startup.on = False  # turned off in Task Manager
    page._show_startup()  # what opening the page does
    assert not page.startup.isChecked()
    startup.fail = True
    page.startup.setChecked(True)
    assert not startup.on
    assert not page.startup.isChecked()  # Windows refused, so the switch goes back


# --- Ethernet ---------------------------------------------------------------------------


def test_the_dashboard_on_a_cable(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    parts["netinfo"].cable = cable_gateway()
    check(qtbot, controller)
    window.dashboard.refresh()
    card = window.dashboard.current
    assert card.name.text() == "ZTE"
    assert card.network.text().startswith("Ethernet · gateway 192.168.1.1")
    assert (card.signal.value.text(), card.signal.note.text()) == ("Cable", "no Wi-Fi signal")
    assert card.recommend_text.isHidden() and card.switch_button.isHidden()
    button = window.dashboard.test_all_button
    assert not button.isEnabled() and button.toolTip() == ON_ETHERNET
    assert "ZTE (Ethernet): Stable" in tray_tooltip(controller.last_snapshot.report, Status(
        StatusLevel.GOOD, "Stable"))  # fmt: skip


def test_the_dashboard_says_when_a_vpn_is_on(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    parts["netinfo"].vpn = True
    parts["ping"].blocked = {(WIFI_IP, t) for t in ("1.1.1.1", "8.8.8.8", "142.250.0.1")}
    check(qtbot, controller)
    window.dashboard.refresh()
    card = window.dashboard.current
    assert card.network.text().endswith("VPN on, internet measured through it")
    assert card.internet.note.text().endswith("· VPN")


def test_the_connection_setting(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    box = window.settings_page.connection
    assert [box.itemText(i) for i in range(box.count())] == [
        "Automatic", "Wi-Fi only", "Ethernet only"
    ]  # fmt: skip
    assert box.currentText() == "Automatic"
    box.setCurrentIndex(2)
    assert controller.settings.connection is LinkChoice.ETHERNET


def test_add_the_router_youre_connected_to(qtbot, app_parts) -> None:
    controller, window, parts = app_parts
    parts["netinfo"].cable = cable_gateway(GW, "12-34-56-78-9A-BC")  # a new router
    dialog = RouterDialog(controller, parent=window)
    dialog.use_current.click()
    qtbot.waitUntil(lambda: dialog.use_current.isEnabled(), timeout=TIMEOUT)
    assert [str(m) for m in dialog._macs] == ["12-34-56-78-9A-BC"]
    assert dialog.mac_feedback.text() == (
        "✓ Added 12-34-56-78-9A-BC, the router you're connected to over Ethernet."
    )
    parts["netinfo"].cable = cable_gateway()  # the ZTE
    dialog.use_current.click()
    qtbot.waitUntil(lambda: dialog.use_current.isEnabled(), timeout=TIMEOUT)
    assert dialog.mac_feedback.text() == "You're connected to ZTE, which you've already added."
    assert len(dialog._macs) == 1
    parts["netinfo"].cable = parts["netinfo"].wifi = None
    dialog.use_current.click()
    qtbot.waitUntil(lambda: dialog.use_current.isEnabled(), timeout=TIMEOUT)
    assert dialog.mac_feedback.text() == "You're not connected to a router right now."
    dialog.reject()


@pytest.fixture
def ethernet_pc(qtbot, tmp_path):
    """A PC without a Wi-Fi adapter, plugged into the ZTE."""
    parts = network_parts()
    parts["wifi"] = FakeWifi(missing=True)
    parts["netinfo"] = FakeNetInfo(cable=cable_gateway())
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"],
        FakeWatcher(), None, has_wifi=False,
    )  # fmt: skip
    settings = Settings(pings_per_target=4, routers=sample_routers(), first_run_done=True)
    controller = AppController(settings, tmp_path / "settings.json", parts["store"], services)
    window = MainWindow(controller)
    qtbot.addWidget(window)
    yield controller, window, parts
    window.prepare_quit()
    controller.shutdown()


def test_a_pc_without_wifi_hides_the_wifi_parts(qtbot, ethernet_pc) -> None:
    controller, window, _ = ethernet_pc
    check(qtbot, controller)
    window.dashboard.refresh()
    assert window.dashboard.current.status.title.text() == "Stable"
    assert window.dashboard.wifi_bar.isHidden()  # "no Wi-Fi adapter" is no error here
    assert window.dashboard.test_all_button.isHidden()
    page = window.settings_page
    assert page.location_card.isHidden()
    assert page.test_all_card.isHidden() and page.auto_switch_card.isHidden()

    first_run = FirstRunDialog(controller, lambda: None, window)
    assert first_run.stack.currentIndex() == 1  # no location step
    assert first_run.step_label.text() == "Welcome to Router Checker · step 1 of 2"
    assert first_run.cancelButton.text() == "Skip setup"
    first_run.reject()

    dialog = RouterDialog(controller, parent=window)
    assert dialog.network_list.isHidden()
    assert dialog.scan_note.text().startswith("This PC has no Wi-Fi adapter")
    dialog.reject()


# --- a middle router ----------------------------------------------------------------------


@pytest.fixture
def middle_app(qtbot, tmp_path):
    """On a cable into the middle router, which is on the ZTE."""
    parts = middle_parts()
    services = Services(
        parts["wifi"], parts["ping"], parts["dns"], parts["netinfo"], parts["clock"],
        FakeWatcher(), parts["switcher"], middle=parts["middle"],
    )  # fmt: skip
    settings = Settings(
        pings_per_target=4, routers=middle_routers(), first_run_done=True,
        middle_router=f"{MIDDLE}:8080",
    )  # fmt: skip
    controller = AppController(
        settings, tmp_path / "settings.json", parts["store"], services,
        switch_timing=SwitchTiming(1.0, 0.0, 0.01),
    )  # fmt: skip
    window = MainWindow(controller)
    qtbot.addWidget(window)
    yield controller, window, parts
    window.prepare_quit()
    controller.shutdown()


def test_the_dashboard_behind_the_middle_router(qtbot, middle_app) -> None:
    controller, window, _ = middle_app
    check(qtbot, controller)
    window.dashboard.refresh()
    card = window.dashboard.current
    assert card.name.text() == "ZTE"
    assert card.network.text() == f"Ethernet · middle router {MIDDLE} · router {ZTE_ADDRESS}"
    assert (card.gateway.value.text(), card.gateway.note.text()) == (
        "2.0 ms",
        "middle router 1.0 ms",
    )
    button = window.dashboard.test_all_button
    assert not button.isHidden() and button.isEnabled()


def test_an_unknown_router_behind_the_middle_router_can_be_added(qtbot, middle_app) -> None:
    controller, window, parts = middle_app
    parts["ping"].upstream = "172.16.0.1"
    check(qtbot, controller)
    window.dashboard.refresh()
    card = window.dashboard.current
    assert card.status.title.text() == "Unknown router"
    assert card._prefill.address == "172.16.0.1"
    dialog = RouterDialog(controller, prefill=card._prefill, parent=window)
    assert dialog.address.text() == "172.16.0.1" and not dialog.address.isHidden()
    dialog.name.setText("Cafe")
    assert dialog.validate()
    assert dialog.router().address == "172.16.0.1"
    dialog.address.setText("172.16.0")
    assert not dialog.validate() and "isn't an address like" in dialog.error.text()
    dialog.address.setText(ZTE_ADDRESS)
    assert (
        not dialog.validate()
        and dialog.error.text() == f"ZTE already has the address {ZTE_ADDRESS}."
    )
    dialog.reject()


def test_the_middle_router_setting(qtbot, middle_app) -> None:
    controller, window, _ = middle_app
    page = window.settings_page
    assert page.middle.text() == f"{MIDDLE}:8080"
    page.middle.setText("http://192.168.8.2:81/")
    page._set_middle()
    assert controller.settings.middle_router == "192.168.8.2:81"
    page.middle.setText("192.168.8.2:port")
    page._set_middle()
    assert page.middle.isError() and controller.settings.middle_router == "192.168.8.2:81"
    page.middle.setText("")
    page._set_middle()
    assert controller.settings.middle_router == ""
    page.middle.setText(f"{MIDDLE}:8080")
    page._test_middle()
    qtbot.waitUntil(lambda: page.middle_test.isEnabled(), timeout=TIMEOUT)
    bars = window.findChildren(InfoBar)
    assert any(b.title == "The middle router answers" for b in bars)


def test_the_address_field_is_hidden_without_a_middle_router(qtbot, app_parts) -> None:
    controller, window, _ = app_parts
    dialog = RouterDialog(controller, parent=window)
    assert dialog.address.isHidden()
    dialog.reject()


def test_use_the_router_im_connected_to_behind_the_middle_router(qtbot, middle_app) -> None:
    controller, window, parts = middle_app
    parts["ping"].upstream = "172.16.0.1"  # a router that isn't added yet
    dialog = RouterDialog(controller, parent=window)
    dialog.use_current.click()
    qtbot.waitUntil(lambda: dialog.use_current.isEnabled(), timeout=TIMEOUT)
    assert dialog.address.text() == "172.16.0.1" and dialog._macs == []  # not the middle's MAC
    assert dialog.mac_feedback.text().startswith("✓ Your middle router is on the router at")
    parts["ping"].upstream = ZTE_ADDRESS
    dialog.use_current.click()
    qtbot.waitUntil(lambda: dialog.use_current.isEnabled(), timeout=TIMEOUT)
    assert dialog.mac_feedback.text() == "Your middle router is on ZTE, which you've already added."
    dialog.reject()
