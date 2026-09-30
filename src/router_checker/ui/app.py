"""Start the desktop app: ``uv run router-checker-app`` (no console window)."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
import threading
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtCore import QTimer, QtMsgType, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from router_checker import __version__
from router_checker.core.settings import Settings, load_settings
from router_checker.core.storage import SqliteHistoryStore

APP_ID = "RouterChecker.RouterChecker"
APP_NAME = "Router Checker"
log = logging.getLogger("router_checker")


def setup_logging(data_dir: Path) -> None:
    logs = data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        logs / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr is not None:  # a console exists (python -m router_checker.ui)
        root.addHandler(logging.StreamHandler())

    def excepthook(exc_type, exc, tb) -> None:
        log.critical("unhandled error", exc_info=(exc_type, exc, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda args: log.critical(
        "unhandled error in a thread", exc_info=(args.exc_type, args.exc_value, args.exc_traceback)
    )
    qt_levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    qInstallMessageHandler(
        lambda kind, _ctx, message: logging.getLogger("qt").log(
            qt_levels.get(kind, logging.WARNING), message
        )
    )


def load_or_reset(path: Path) -> tuple[Settings, str | None]:
    """Settings from disk; an unreadable file is kept aside and defaults are used."""
    try:
        return load_settings(path) or Settings(), None
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("could not read %s", path)
        backup = path.with_name(path.name + ".bad")
        try:
            os.replace(path, backup)
        except OSError:
            log.exception("could not move the broken settings file aside")
        message = f"Your settings couldn't be read, so defaults are used. The old file is {backup}."
        return Settings(), message


def make_toaster(data_dir: Path):
    """Windows notifications under the app's own name and icon, or None (tray messages)."""
    from router_checker.ui.style import app_icon

    try:
        from router_checker.platform_windows.toasts import WindowsToastNotifier, register_app_id

        icon = data_dir / "app-icon.png"
        if not app_icon().pixmap(256, 256).save(str(icon)):
            icon = None
        register_app_id(APP_ID, APP_NAME, icon)
        return WindowsToastNotifier(APP_ID, APP_NAME)
    except Exception:
        log.warning("Windows notifications are unavailable; using tray messages", exc_info=True)
        return None


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="router-checker-app")
    parser.add_argument("--minimized", action="store_true", help="start in the tray")
    parser.add_argument("--data-dir", type=Path, help="default: %%LOCALAPPDATA%%\\RouterChecker")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    from router_checker.platform_windows import services as win

    data_dir = args.data_dir or win.data_dir()
    setup_logging(data_dir)
    log.info("Router Checker %s starting (data in %s)", __version__, data_dir)
    try:
        win.set_app_user_model_id(APP_ID)
    except OSError:
        log.warning("could not set the app id", exc_info=True)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)

    from router_checker.ui.single_instance import SingleInstance

    instance = SingleInstance(f"RouterChecker-{getpass.getuser()}")
    if instance.already_running():
        log.info("already running; asked the other copy to show itself")
        return 0

    from qfluentwidgets import Theme, setTheme, setThemeColor
    from qframelesswindow.utils import getSystemAccentColor

    setTheme(Theme.AUTO)
    try:
        setThemeColor(getSystemAccentColor(), save=False)
    except Exception:
        log.warning("could not read the Windows accent color", exc_info=True)

    from router_checker.platform_windows.icmp import WindowsPingService
    from router_checker.platform_windows.idle import WindowsIdleMonitor
    from router_checker.platform_windows.netinfo import WindowsNetworkInfoService
    from router_checker.platform_windows.netwatch import RouteChangeWatcher
    from router_checker.platform_windows.wlan import WindowsWifiService
    from router_checker.ui.controller import AppController, Services
    from router_checker.ui.main_window import MainWindow
    from router_checker.ui.notifications import NotificationCenter
    from router_checker.ui.tray import TrayIcon

    settings_path = data_dir / "settings.json"
    settings, warning = load_or_reset(settings_path)
    store = SqliteHistoryStore(data_dir / "history.db")
    wifi = WindowsWifiService()
    services = Services(
        wifi=wifi,
        ping=WindowsPingService(),
        dns=win.SocketDnsService(),
        netinfo=WindowsNetworkInfoService(),
        clock=win.SystemClock(),
        watcher=RouteChangeWatcher(),
        switcher=wifi,
        idle=WindowsIdleMonitor(),
    )
    controller = AppController(settings, settings_path, store, services)
    window = MainWindow(controller)
    window.start_theme_listener()
    tray = TrayIcon(controller, window)
    tray.show()
    instance.activated.connect(window.bring_to_front)
    notifications = NotificationCenter(controller, tray, make_toaster(data_dir), window)
    notifications.openRequested.connect(window.bring_to_front)
    notifications.switchRequested.connect(window.switch_to)
    notifications.statusChanged.connect(window.settings_page.show_notification_status)
    window.hiddenToTray.connect(notifications.notify_hidden)
    window.settings_page.testNotificationRequested.connect(notifications.send_test)
    window.settings_page.notificationStatusRequested.connect(notifications.refresh_status)

    def quit_app() -> None:
        log.info("exiting")
        window.prepare_quit()
        tray.hide()
        window.hide()
        app.quit()

    tray.exitRequested.connect(quit_app)
    window.exitRequested.connect(quit_app)

    if not settings.first_run_done:
        window.show()
        QTimer.singleShot(400, window.run_first_run)
    elif not args.minimized:
        window.show()
    if warning:
        QTimer.singleShot(800, lambda: window.show_warning("Settings were reset", warning))

    controller.start()
    code = app.exec()
    controller.shutdown()
    wifi.close()
    store.close()
    log.info("stopped")
    return code
