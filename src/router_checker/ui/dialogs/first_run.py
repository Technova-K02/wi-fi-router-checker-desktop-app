"""First run, in 3 steps: allow location, add routers, choose the interval."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    MessageBoxBase,
    PushButton,
    SubtitleLabel,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker.core.presentation import StatusLevel
from router_checker.core.scheduler import INTERVAL_CHOICES_MIN
from router_checker.core.settings import Settings
from router_checker.ui.controller import AppController
from router_checker.ui.shell import open_location_settings
from router_checker.ui.widgets import ColorDot, StatusIcon

STEPS = 3


def _text(text: str, parent: QWidget) -> BodyLabel:
    label = BodyLabel(text, parent)
    label.setWordWrap(True)
    return label


class FirstRunDialog(MessageBoxBase):
    def __init__(
        self,
        controller: AppController,
        add_router: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.widget.setMinimumWidth(620)
        self.step_label = CaptionLabel("", self)
        self.stack = QStackedWidget(self)

        # Step 1: location
        location = QWidget(self.stack)
        v = QVBoxLayout(location)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(SubtitleLabel("Allow location access", location))
        v.addWidget(
            _text(
                "Windows only lets desktop apps see Wi-Fi networks when location access is on. "
                "Router Checker uses it to find your routers in Wi-Fi scans. It never looks up "
                "where you are, and nothing leaves this PC.",
                location,
            )
        )
        v.addWidget(
            _text(
                "Without it, the router you're connected to is still tested, but your other "
                "routers can't be seen.",
                location,
            )
        )
        status_row = QHBoxLayout()
        self.location_icon = StatusIcon(18, location)
        self.location_text = BodyLabel("Checking…", location)
        status_row.addWidget(self.location_icon)
        status_row.addWidget(self.location_text, 1)
        v.addLayout(status_row)
        buttons = QHBoxLayout()
        open_settings = PushButton("Open location settings", location)
        open_settings.clicked.connect(open_location_settings)
        check = PushButton("Check again", location)
        check.clicked.connect(controller.probe_location)
        buttons.addWidget(open_settings)
        buttons.addWidget(check)
        buttons.addStretch(1)
        v.addLayout(buttons)
        v.addStretch(1)

        # Step 2: routers
        routers = QWidget(self.stack)
        v = QVBoxLayout(routers)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(SubtitleLabel("Add your routers", routers))
        v.addWidget(
            _text(
                "Add each router you can connect to. Pick it from the nearby networks, or type "
                "its MAC address. You can add more later on the Routers page.",
                routers,
            )
        )
        self.router_rows = QVBoxLayout()
        self.router_rows.setSpacing(6)
        v.addLayout(self.router_rows)
        add = PushButton(FIF.ADD, "Add router", routers)
        add.clicked.connect(add_router)
        add_row = QHBoxLayout()
        add_row.addWidget(add)
        add_row.addStretch(1)
        v.addLayout(add_row)
        v.addStretch(1)

        # Step 3: interval
        interval = QWidget(self.stack)
        v = QVBoxLayout(interval)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)
        v.addWidget(SubtitleLabel("How often should it check?", interval))
        v.addWidget(
            _text(
                "Router Checker tests the router you're connected to on this interval, and "
                "every minute while it's unstable. A check is a few pings and a Wi-Fi scan.",
                interval,
            )
        )
        self.interval = ComboBox(interval)
        self.interval.setAccessibleName("Check interval")
        for minutes in INTERVAL_CHOICES_MIN:
            label = f"Every {minutes} minute{'s' if minutes > 1 else ''}"
            self.interval.addItem(
                label + (" (recommended)" if minutes == 5 else ""), userData=minutes
            )
        self.interval.setCurrentIndex(
            max(0, self.interval.findData(controller.settings.interval_min))
        )
        v.addWidget(self.interval, 0, Qt.AlignmentFlag.AlignLeft)
        v.addWidget(CaptionLabel("You can change this later in Settings.", interval))
        v.addStretch(1)

        for page in (location, routers, interval):
            self.stack.addWidget(page)
        self.stack.setMinimumHeight(260)
        self.viewLayout.addWidget(self.step_label)
        self.viewLayout.addWidget(self.stack)

        self.cancelButton.clicked.disconnect()
        self.cancelButton.clicked.connect(self._back)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)

        controller.locationStatus.connect(self._show_location)
        controller.settingsChanged.connect(self._show_routers)
        QGuiApplication.instance().applicationStateChanged.connect(self._app_state_changed)
        self._show_routers(controller.settings)
        self._set_step(0)

    def interval_min(self) -> int:
        return self.interval.currentData()

    def validate(self) -> bool:
        step = self.stack.currentIndex()
        if step < STEPS - 1:
            self._set_step(step + 1)
            return False
        return True

    def _back(self) -> None:
        step = self.stack.currentIndex()
        if step == 0:
            self.reject()
        else:
            self._set_step(step - 1)

    def _set_step(self, step: int) -> None:
        self.stack.setCurrentIndex(step)
        self.step_label.setText(f"Welcome to Router Checker · step {step + 1} of {STEPS}")
        self.yesButton.setText("Finish" if step == STEPS - 1 else "Next")
        self.cancelButton.setText("Skip setup" if step == 0 else "Back")
        if step == 0:
            self.controller.probe_location()

    def _app_state_changed(self, state: Qt.ApplicationState) -> None:
        # Back from the Settings app: check the permission again.
        if state == Qt.ApplicationState.ApplicationActive and self.stack.currentIndex() == 0:
            self.controller.probe_location()

    def _show_location(self, allowed: bool | None) -> None:
        if allowed:
            self.location_icon.set_level(StatusLevel.GOOD)
            self.location_text.setText("Location access is on. You're all set for this step.")
        elif allowed is None:
            self.location_icon.set_level(StatusLevel.UNKNOWN)
            self.location_text.setText("Couldn't check. Is the Wi-Fi adapter turned on?")
        else:
            self.location_icon.set_level(StatusLevel.WARNING)
            self.location_text.setText(
                "Location access is off. Open the settings and turn on “Location services” "
                "and “Let desktop apps access your location”."
            )

    def _show_routers(self, settings: Settings) -> None:
        while self.router_rows.count():
            item = self.router_rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        parent = self.stack.widget(1) or self
        if not settings.routers:
            self.router_rows.addWidget(CaptionLabel("No routers yet.", parent))
        for router in settings.routers:
            row = QWidget(parent)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(10)
            layout.addWidget(ColorDot(router.color, 12, row))
            layout.addWidget(BodyLabel(router.name, row))
            details = (
                f'Wi-Fi "{router.ssid}"' if router.ssid else ", ".join(map(str, router.macs[:2]))
            )
            layout.addWidget(CaptionLabel(details, row), 1)
            self.router_rows.addWidget(row)
