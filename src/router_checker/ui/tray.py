"""Tray icon (status color and shape), its flyout and its menu."""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QSystemTrayIcon, QVBoxLayout, QWidget
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    Flyout,
    FlyoutAnimationType,
    FlyoutViewBase,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SystemTrayMenu,
)
from qfluentwidgets import FluentIcon as FIF

from router_checker.core.models import Alert, AlertKind
from router_checker.core.presentation import (
    DASH,
    StatusLevel,
    current_metrics,
    fmt_clock,
    fmt_countdown,
    fmt_ms,
    overall_status,
    recommendation_text,
    score_text,
    tray_tooltip,
)
from router_checker.ui.controller import AppController
from router_checker.ui.style import status_icon
from router_checker.ui.widgets import StatusIcon

REOPEN_GUARD_S = 0.35  # a tray click that closed the flyout must not reopen it


class TrayFlyoutView(FlyoutViewBase):
    """Current router, score, gateway and internet ping, recommendation, buttons."""

    def __init__(self, controller: AppController, on_open, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setFixedWidth(340)
        self.icon = StatusIcon(22, self)
        self.title = StrongBodyLabel(self)
        self.detail = CaptionLabel(self)
        self.detail.setWordWrap(True)
        self.values: dict[str, BodyLabel] = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        for row, name in enumerate(("Score", "Gateway ping", "Internet ping", "Recommended")):
            grid.addWidget(CaptionLabel(name, self), row, 0)
            value = BodyLabel(DASH, self)
            value.setWordWrap(True)
            grid.addWidget(value, row, 1)
            self.values[name] = value
        self.when = CaptionLabel(self)
        self.check_button = PrimaryPushButton(FIF.SYNC, "Check now", self)
        self.check_button.clicked.connect(controller.check_now)
        self.cancel_button = PushButton(FIF.CLOSE, "Cancel Test all", self)
        self.cancel_button.clicked.connect(lambda: controller.cancel_test_all())
        open_button = PushButton(FIF.HOME, "Open", self)
        open_button.clicked.connect(on_open)

        header = QHBoxLayout()
        header.setSpacing(10)
        header.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        header.addWidget(self.title, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.check_button, 1)
        buttons.addWidget(self.cancel_button, 1)
        buttons.addWidget(open_button, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self.detail)
        layout.addLayout(grid)
        layout.addWidget(self.when)
        layout.addLayout(buttons)

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=lambda: self.window().close())
        for signal in (
            controller.checkStarted,
            controller.checkFinished,
            controller.checkFailed,
            controller.settingsChanged,
            controller.activityChanged,
        ):
            signal.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self.refresh()

    def refresh(self, *_args: object) -> None:
        c = self.controller
        report = c.last_snapshot.report if c.last_snapshot else None
        status = overall_status(report, c.last_failure)
        router = report.match.router if report else None
        name = next((r.name for r in c.settings.routers if router and r.id == router.id), None)
        self.icon.set_level(status.level)
        self.title.setText(f"{name} · {status.title}" if name else status.title)
        self.detail.setText(status.detail)
        self.detail.setVisible(bool(status.detail))
        current = report.current if report else None
        metrics = current_metrics(report)
        self.values["Score"].setText(score_text(current.score if current else None))
        gateway = DASH
        if metrics is not None:
            gateway = "no reply" if metrics.gateway_silent else fmt_ms(metrics.gateway_ms)
        self.values["Gateway ping"].setText(gateway)
        self.values["Internet ping"].setText(fmt_ms(metrics.internet_ms) if metrics else DASH)
        rec = report.recommendation if report else None
        self.values["Recommended"].setText(
            recommendation_text(rec, c.settings.routers) if rec else "None right now"
        )
        state = c.run_state
        testing = state is not None and state.kind == "test_all"
        self.check_button.setVisible(not testing)
        self.check_button.setEnabled(not c.is_busy)
        self.cancel_button.setVisible(testing)
        self.cancel_button.setEnabled(bool(state and state.can_cancel))
        self.setAccessibleName(f"{self.title.text()}. {status.detail}")
        self._tick()

    def _tick(self) -> None:
        c = self.controller
        if c.activity is not None:
            self.when.setText(c.activity)
            return
        if c.is_checking:
            self.when.setText("Checking now…")
            return
        now = c.now()
        parts = []
        if c.last_snapshot is not None:
            parts.append(f"Last check {fmt_clock(c.last_snapshot.report.timestamp, now)}")
        if c.next_check_at is not None:
            parts.append(f"next in {fmt_countdown((c.next_check_at - now).total_seconds())}")
        self.when.setText(" · ".join(parts))


class TrayIcon(QObject):
    exitRequested = Signal()

    def __init__(self, controller: AppController, window) -> None:
        super().__init__(window)
        self.controller = controller
        self.window = window
        self._icons = {level: status_icon(level) for level in StatusLevel}
        self.tray = QSystemTrayIcon(self._icons[StatusLevel.UNKNOWN], self)
        self.menu = SystemTrayMenu(parent=window)
        self.menu.addActions(
            [
                Action(FIF.HOME, "Open Router Checker", triggered=window.bring_to_front),
                Action(FIF.SYNC, "Check now", triggered=controller.check_now),
            ]
        )
        if controller.can_switch:
            self.menu.addAction(
                Action(FIF.ROTATE, "Test all now", triggered=lambda: window.test_all())
            )
        self.menu.addSeparator()
        # Not triggered=self.exitRequested.emit: "triggered" also passes a "checked" flag.
        self.menu.addAction(
            Action(FIF.POWER_BUTTON, "Exit", triggered=lambda: self.exitRequested.emit())
        )
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.messageClicked.connect(window.bring_to_front)
        self._flyout: Flyout | None = None
        self._closed_at = 0.0
        for signal in (
            controller.checkStarted,
            controller.checkFinished,
            controller.checkFailed,
            controller.settingsChanged,
            controller.activityChanged,
        ):
            signal.connect(self.refresh)
        self.refresh()

    def show(self) -> None:
        self.tray.show()

    def hide(self) -> None:
        self.close_flyout()
        self.tray.hide()

    def refresh(self, *_args: object) -> None:
        c = self.controller
        report = c.last_snapshot.report if c.last_snapshot else None
        status = overall_status(report, c.last_failure)
        self.tray.setIcon(self._icons[status.level])
        self.tray.setToolTip(tray_tooltip(report, status, c.is_checking, c.activity))

    # Balloon messages: the fallback when Windows notifications (toasts) don't work.

    def show_message(
        self, title: str, text: str, level: StatusLevel = StatusLevel.UNKNOWN, ms: int = 6_000
    ) -> None:
        self.tray.showMessage(title, text, self._icons[level], ms)

    def notify_alert(self, alert: Alert) -> None:
        good = alert.kind is AlertKind.RECOVERED
        self.show_message(
            alert.title, alert.text, StatusLevel.GOOD if good else StatusLevel.BAD, 10_000
        )

    # --- flyout -------------------------------------------------------------------

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_flyout()
        elif reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.close_flyout()
            self.window.bring_to_front()

    def toggle_flyout(self) -> None:
        if self._flyout is not None:
            self.close_flyout()
            return
        if time.monotonic() - self._closed_at < REOPEN_GUARD_S:
            return
        view = TrayFlyoutView(self.controller, self._open_window)
        flyout = Flyout(view, None, isDeleteOnClose=True)
        flyout.closed.connect(self._on_flyout_closed)
        self._flyout = flyout
        flyout.adjustSize()
        pos, animation = self._flyout_position(flyout)
        flyout.exec(pos, animation)
        flyout.activateWindow()
        flyout.raise_()

    def close_flyout(self) -> None:
        if self._flyout is not None:
            self._flyout.close()

    def _on_flyout_closed(self) -> None:
        self._flyout = None
        self._closed_at = time.monotonic()

    def _open_window(self) -> None:
        self.close_flyout()
        self.window.bring_to_front()

    def _flyout_position(self, flyout: Flyout) -> tuple[QPoint, FlyoutAnimationType]:
        """Above the tray icon when the taskbar is at the bottom, else below it."""
        size = flyout.sizeHint()
        geometry = self.tray.geometry()
        valid = geometry.isValid() and not geometry.isEmpty()
        anchor = geometry.center() if valid else QCursor.pos()
        screen = QGuiApplication.screenAt(anchor) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry()
        x = anchor.x() - size.width() // 2
        if anchor.y() >= available.center().y():
            return QPoint(x, available.bottom() - size.height()), FlyoutAnimationType.PULL_UP
        return QPoint(x, available.top()), FlyoutAnimationType.DROP_DOWN
