"""Settings: monitoring, instability rules, notifications, privacy and data.

Changes apply right away (spin boxes after a short pause) and are saved by the
controller.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    DoubleSpinBox,
    ExpandGroupSettingCard,
    InfoBar,
    LineEdit,
    PushButton,
    SettingCard,
    SettingCardGroup,
    SpinBox,
    SwitchButton,
    ToolTipFilter,
    TransparentToolButton,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker import __version__
from router_checker.core.alerts import Thresholds
from router_checker.core.scheduler import INTERVAL_CHOICES_MIN
from router_checker.core.settings import Settings, is_valid_target
from router_checker.ui.controller import AppController
from router_checker.ui.pages.base import Page
from router_checker.ui.shell import open_folder, open_location_settings

APPLY_DELAY_MS = 600


def _card(icon: FIF, title: str, content: str, *controls: QWidget) -> SettingCard:
    card = SettingCard(icon, title, content)
    for control in controls:
        card.hBoxLayout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight)
        card.hBoxLayout.addSpacing(8)
        if not control.accessibleName():
            control.setAccessibleName(title)
    card.hBoxLayout.addSpacing(8)
    return card


def _spin(low: int, high: int, suffix: str, step: int = 1) -> SpinBox:
    box = SpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setSuffix(suffix)
    box.setMinimumWidth(150)
    return box


class TargetsEditor(QWidget):
    """List of internet targets with Remove buttons and an Add field."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._targets: list[str] = []
        self.rows_host = QWidget(self)
        self.rows = QVBoxLayout(self.rows_host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(2)
        self.edit = LineEdit(self)
        self.edit.setPlaceholderText("IPv4 address or host name, e.g. 9.9.9.9")
        self.edit.setClearButtonEnabled(True)
        self.edit.setAccessibleName("New internet target")
        self.edit.returnPressed.connect(self._add)
        self.edit.textChanged.connect(lambda _t: self._show_error(""))
        add = PushButton(FIF.ADD, "Add", self)
        add.clicked.connect(self._add)
        self.error = CaptionLabel("", self)
        self.error.setTextColor(QColor("#C42B1C"), QColor("#FF99A4"))
        self.error.hide()

        add_row = QHBoxLayout()
        add_row.addWidget(self.edit, 1)
        add_row.addWidget(add)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 12, 48, 14)
        layout.setSpacing(8)
        layout.addWidget(self.rows_host)
        layout.addLayout(add_row)
        layout.addWidget(self.error)

    def targets(self) -> tuple[str, ...]:
        return tuple(self._targets)

    def set_targets(self, targets: tuple[str, ...]) -> None:
        self._targets = list(targets)
        self._rebuild()

    def _rebuild(self) -> None:
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for target in self._targets:
            row = QWidget(self.rows_host)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(BodyLabel(target, row), 1)
            remove = TransparentToolButton(FIF.DELETE, row)
            remove.setToolTip(f"Remove {target}")
            remove.setAccessibleName(f"Remove {target}")
            remove.installEventFilter(ToolTipFilter(remove))
            remove.setEnabled(len(self._targets) > 1)
            remove.clicked.connect(lambda _c=False, t=target: self._remove(t))
            layout.addWidget(remove)
            self.rows.addWidget(row)
        self.rows_host.adjustSize()
        self.adjustSize()

    def _show_error(self, text: str) -> None:
        self.error.setText(text)
        self.error.setVisible(bool(text))

    def _add(self) -> None:
        text = self.edit.text().strip()
        if not text:
            return
        if not is_valid_target(text):
            self._show_error("Use an IPv4 address or a host name, like 9.9.9.9 or example.com.")
            return
        if text.lower() in (t.lower() for t in self._targets):
            self._show_error(f"{text} is already in the list.")
            return
        self._targets.append(text)
        self.edit.clear()
        self._rebuild()
        self.changed.emit()

    def _remove(self, target: str) -> None:
        if len(self._targets) > 1:
            self._targets.remove(target)
            self._rebuild()
            self.changed.emit()


class SettingsPage(Page):
    def __init__(self, controller: AppController, parent: QWidget | None = None) -> None:
        super().__init__("settingsPage", "Settings", parent)
        self.controller = controller
        self._loading = False
        self._delay = QTimer(self)
        self._delay.setSingleShot(True)
        self._delay.setInterval(APPLY_DELAY_MS)
        self._delay.timeout.connect(self._apply)

        # Monitoring
        self.interval = ComboBox()
        for minutes in INTERVAL_CHOICES_MIN:
            self.interval.addItem(f"{minutes} minute{'s' if minutes > 1 else ''}", userData=minutes)
        self.interval.currentIndexChanged.connect(lambda _i: self._apply())
        self.network_change = SwitchButton()
        self.network_change.checkedChanged.connect(lambda _c: self._apply())
        self.targets = TargetsEditor()
        self.targets.changed.connect(self._targets_changed)
        self.pings = _spin(3, 30, " pings")

        monitoring = SettingCardGroup("Monitoring", self.view)
        monitoring.addSettingCard(
            _card(
                FIF.STOP_WATCH,
                "Check interval",
                "How often the current router is tested. Every minute while it's unstable.",
                self.interval,
            )
        )
        monitoring.addSettingCard(
            _card(
                FIF.SYNC,
                "Check when the network changes",
                "Test a few seconds after Windows switches to another network.",
                self.network_change,
            )
        )
        self.targets_card = ExpandGroupSettingCard(
            FIF.GLOBE,
            "Internet targets",
            "Pinged on every check to measure the internet connection.",
        )
        self.targets_card.addGroupWidget(self.targets)
        monitoring.addSettingCard(self.targets_card)
        monitoring.addSettingCard(
            _card(
                FIF.SPEED_HIGH,
                "Pings per target",
                "More pings give steadier numbers but take a little longer.",
                self.pings,
            )
        )

        # Instability rules
        self.loss = DoubleSpinBox()
        self.loss.setRange(1, 50)
        self.loss.setDecimals(0)
        self.loss.setSuffix(" %")
        self.loss.setMinimumWidth(150)
        self.p95 = _spin(10, 1000, " ms", 10)
        self.jitter = _spin(5, 500, " ms", 5)
        self.unstable = _spin(1, 10, " checks")
        self.recovery = _spin(1, 10, " checks")
        self.cooldown = _spin(1, 240, " min", 5)
        rules = SettingCardGroup("When is a router unstable?", self.view)
        rules.addSettingCard(
            _card(FIF.REMOVE_FROM, "Packet loss", "Unstable at or above this loss.", self.loss)
        )
        rules.addSettingCard(
            _card(
                FIF.SPEED_MEDIUM,
                "Gateway latency (p95)",
                "Unstable when the slowest 5% of pings to the router take longer than this.",
                self.p95,
            )
        )
        rules.addSettingCard(
            _card(
                FIF.SPEED_OFF,
                "Jitter",
                "Unstable above this variation between pings.",
                self.jitter,
            )
        )
        rules.addSettingCard(
            _card(
                FIF.RINGER,
                "Alert after",
                "Unstable checks in a row before you're alerted.",
                self.unstable,
            )
        )
        rules.addSettingCard(
            _card(
                FIF.ACCEPT,
                "Back to normal after",
                "Good checks in a row before the all-clear.",
                self.recovery,
            )
        )
        rules.addSettingCard(
            _card(
                FIF.HISTORY,
                "Quiet time between alerts",
                "Per router, so a flaky router doesn't keep alerting.",
                self.cooldown,
            )
        )

        # Notifications
        self.notify = SwitchButton()
        self.notify.checkedChanged.connect(lambda _c: self._apply())
        notifications = SettingCardGroup("Notifications", self.view)
        notifications.addSettingCard(
            _card(
                FIF.MEGAPHONE,
                "Alerts",
                "Show a notification when the current router becomes unstable or recovers.",
                self.notify,
            )
        )

        # Privacy and data
        open_location = PushButton("Open settings")
        open_location.clicked.connect(open_location_settings)
        check_location = PushButton("Check again")
        check_location.clicked.connect(self._probe_location)
        self.location_card = _card(
            FIF.WIFI, "Location access", "Checking…", open_location, check_location
        )
        self.retention = _spin(1, 365, " days")
        open_data = PushButton("Open folder")
        data_dir = controller.data_dir
        open_data.clicked.connect(lambda: data_dir and open_folder(data_dir))
        open_data.setEnabled(data_dir is not None)
        privacy = SettingCardGroup("Privacy and data", self.view)
        privacy.addSettingCard(self.location_card)
        privacy.addSettingCard(
            _card(
                FIF.CALENDAR,
                "Keep history for",
                "Older checks and events are deleted automatically.",
                self.retention,
            )
        )
        privacy.addSettingCard(
            _card(FIF.FOLDER, "Data folder", str(data_dir or "Not saved"), open_data)
        )
        about = SettingCardGroup("About", self.view)
        about.addSettingCard(
            _card(
                FIF.INFO,
                "Router Checker",
                f"Version {__version__}. Your data stays on this PC.",
            )
        )

        for group in (monitoring, rules, notifications, privacy, about):
            self.body.addWidget(group)

        for box in (self.pings, self.loss, self.p95, self.jitter, self.unstable, self.recovery,
                    self.cooldown, self.retention):  # fmt: skip
            box.valueChanged.connect(self._apply_later)

        controller.settingsChanged.connect(self._load)
        controller.locationStatus.connect(self._show_location)
        self._load(controller.settings)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._probe_location()

    def _probe_location(self) -> None:
        self.location_card.setContent("Checking…")
        self.controller.probe_location()

    def _show_location(self, allowed: bool | None) -> None:
        if allowed is None:
            text = "Unknown. Is the Wi-Fi adapter on?"
        elif allowed:
            text = "On. Router Checker can scan for your other routers."
        else:
            text = "Off. Turn on “Let desktop apps access your location” to see other routers."
        self.location_card.setContent(text)

    def _load(self, settings: Settings) -> None:
        self._loading = True
        try:
            self.interval.setCurrentIndex(max(0, self.interval.findData(settings.interval_min)))
            self.network_change.setChecked(settings.check_on_network_change)
            self.targets.set_targets(settings.targets)
            self.targets_card._adjustViewSize()
            self.pings.setValue(settings.pings_per_target)
            self.loss.setValue(settings.thresholds.loss_pct)
            self.p95.setValue(round(settings.thresholds.gateway_p95_ms))
            self.jitter.setValue(round(settings.thresholds.jitter_ms))
            self.unstable.setValue(settings.unstable_checks)
            self.recovery.setValue(settings.recovery_checks)
            self.cooldown.setValue(settings.alert_cooldown_min)
            self.notify.setChecked(settings.notifications_enabled)
            self.retention.setValue(settings.retention_days)
        finally:
            self._loading = False

    def _targets_changed(self) -> None:
        self.targets_card._adjustViewSize()
        self._apply()

    def _apply_later(self, *_args: object) -> None:
        if not self._loading:
            self._delay.start()

    def _apply(self) -> None:
        if self._loading:
            return
        self._delay.stop()
        current = self.controller.settings
        try:
            new = replace(
                current,
                interval_min=self.interval.currentData(),
                check_on_network_change=self.network_change.isChecked(),
                targets=self.targets.targets(),
                pings_per_target=self.pings.value(),
                thresholds=Thresholds(
                    loss_pct=float(self.loss.value()),
                    gateway_p95_ms=float(self.p95.value()),
                    jitter_ms=float(self.jitter.value()),
                ),
                unstable_checks=self.unstable.value(),
                recovery_checks=self.recovery.value(),
                alert_cooldown_min=self.cooldown.value(),
                notifications_enabled=self.notify.isChecked(),
                retention_days=self.retention.value(),
            )
        except ValueError as exc:
            InfoBar.error("Setting not saved", str(exc), duration=5000, parent=self.window())
            self._load(current)
            return
        if new != current:
            self.controller.update_settings(new)
