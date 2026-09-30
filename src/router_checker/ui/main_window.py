"""Main window: navigation between Dashboard, Routers, History and Settings."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut, QShowEvent
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets import (
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SystemThemeListener,
)

from router_checker.core.models import Router
from router_checker.ui.controller import AppController
from router_checker.ui.dialogs.first_run import FirstRunDialog
from router_checker.ui.dialogs.router_dialog import RouterDialog
from router_checker.ui.pages.dashboard import DashboardPage
from router_checker.ui.pages.history import HistoryPage
from router_checker.ui.pages.routers import RoutersPage
from router_checker.ui.pages.settings import SettingsPage
from router_checker.ui.style import app_icon


class MainWindow(FluentWindow):
    def __init__(self, controller: AppController) -> None:
        super().__init__()
        self.controller = controller
        self.tray = None  # set by the app once the tray icon exists
        self._quitting = False
        self._hidden_hint_shown = False
        self._theme_listener: SystemThemeListener | None = None
        self._shown_once = False

        self.setWindowTitle("Router Checker")
        self.setWindowIcon(app_icon())
        self.resize(1120, 820)
        self.setMinimumSize(820, 600)
        self.navigationInterface.setExpandWidth(210)

        self.dashboard = DashboardPage(controller, self)
        self.routers = RoutersPage(controller, self)
        self.history = HistoryPage(controller, self)
        self.settings_page = SettingsPage(controller, self)
        self.addSubInterface(self.dashboard, FIF.HOME, "Dashboard")
        self.addSubInterface(self.routers, FIF.WIFI, "Routers")
        self.addSubInterface(self.history, FIF.HISTORY, "History")
        self.addSubInterface(
            self.settings_page, FIF.SETTING, "Settings", NavigationItemPosition.BOTTOM
        )

        self.dashboard.addRouterRequested.connect(self.add_router)
        self.dashboard.openRouterRequested.connect(self.show_router)
        self.routers.addRequested.connect(lambda: self.add_router(None))
        self.routers.editRequested.connect(self.edit_router)
        self.routers.deleteRequested.connect(self.delete_router)
        controller.checkFailed.connect(self._show_check_failed)

        pages = (self.dashboard, self.routers, self.history, self.settings_page)
        for number, page in enumerate(pages, start=1):
            QShortcut(
                QKeySequence(f"Ctrl+{number}"), self, activated=lambda p=page: self.switchTo(p)
            )
        QShortcut(QKeySequence(Qt.Key.Key_F5), self, activated=controller.check_now)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=controller.check_now)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=lambda: self.add_router(None))

    # --- window behavior --------------------------------------------------------

    def start_theme_listener(self) -> None:
        """Follow the Windows light/dark setting while the app runs."""
        self._theme_listener = SystemThemeListener(self)
        self._theme_listener.start()

    def prepare_quit(self) -> None:
        self._quitting = True
        if self._theme_listener is not None:
            self._theme_listener.terminate()
            self._theme_listener.wait(1000)
            self._theme_listener = None

    def bring_to_front(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self._shown_once:
            self._shown_once = True
            # Show page names next to the icons when the window is wide enough.
            QTimer.singleShot(0, lambda: self.navigationInterface.expand(useAni=False))

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._quitting:
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()
        if not self._hidden_hint_shown and self.tray is not None:
            self._hidden_hint_shown = True
            self.tray.notify_hidden()

    # --- routers ----------------------------------------------------------------

    def add_router(self, prefill: Router | None = None) -> None:
        self.bring_to_front()
        dialog = RouterDialog(self.controller, prefill=prefill, parent=self)
        if dialog.exec():
            router = dialog.router()
            self.controller.add_router(router)
            self._info(f"Added {router.name}", "It shows up on the dashboard after the next check.")

    def edit_router(self, router_id: str) -> None:
        router = self.controller.settings.router(router_id)
        if router is None:
            return
        dialog = RouterDialog(self.controller, router=router, parent=self)
        if dialog.exec():
            self.controller.update_router(dialog.router())

    def delete_router(self, router_id: str) -> None:
        router = self.controller.settings.router(router_id)
        if router is None:
            return
        box = MessageBox(
            f"Delete {router.name}?",
            "Router Checker stops tracking it. Its history stays until it ages out.",
            self,
        )
        box.yesButton.setText("Delete")
        box.cancelButton.setText("Cancel")
        if box.exec():
            self.controller.remove_router(router_id)
            self.routers.show_list()

    def show_router(self, router_id: str) -> None:
        self.switchTo(self.routers)
        self.routers.show_details(router_id)

    # --- first run and messages -------------------------------------------------

    def run_first_run(self) -> None:
        self.bring_to_front()
        dialog = FirstRunDialog(self.controller, lambda: self.add_router(None), self)
        dialog.exec()
        self.controller.complete_first_run(dialog.interval_min())

    def show_warning(self, title: str, text: str) -> None:
        InfoBar.warning(title, text, duration=-1, position=InfoBarPosition.TOP, parent=self)

    def _info(self, title: str, text: str) -> None:
        InfoBar.success(title, text, duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _show_check_failed(self, message: str) -> None:
        if self.isVisible():
            InfoBar.error(
                "The check failed",
                f"{message}. It will be tried again; details are in the log.",
                duration=8000,
                position=InfoBarPosition.TOP,
                parent=self,
            )
