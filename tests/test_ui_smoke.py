"""Build the real windows with the fake network and render them in both themes."""

import pytest
from qfluentwidgets import Theme, setTheme

from fakes import GW, FakeWatcher, gateway_info, network_parts, sample_routers
from router_checker.core.models import Router, WifiConnection
from router_checker.core.presentation import StatusLevel
from router_checker.core.settings import Settings
from router_checker.ui.controller import AppController, Services
from router_checker.ui.dialogs.first_run import FirstRunDialog
from router_checker.ui.dialogs.router_dialog import RouterDialog
from router_checker.ui.main_window import MainWindow
from router_checker.ui.style import app_icon, status_icon
from router_checker.ui.tray import TrayFlyoutView

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
