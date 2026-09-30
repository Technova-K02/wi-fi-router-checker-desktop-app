"""Alerts and hints as Windows notifications (toasts), with tray balloons as the
fallback when toasts are unavailable.

Quiet hours and the Alerts switch are applied by the controller before an
alert gets here; a test notification and the "still running" hint are shown
because the user just asked for them or just closed the window. When Test all
or a switch couldn't reconnect you, that's a notification too (outside quiet
hours); other results only show in the window.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from router_checker.core.models import Alert, AlertKind
from router_checker.core.presentation import Message, StatusLevel
from router_checker.ui.controller import AppController

log = logging.getLogger(__name__)

OPEN = "open"
CHECK = "check"
SWITCH = "switch:"  # followed by the router id
ALERT_LIFETIME = timedelta(hours=2)  # older alerts leave the notification centre
HINT_LIFETIME = timedelta(minutes=10)


class Toaster(Protocol):
    def show(
        self,
        *,
        tag: str,
        title: str,
        body: str,
        buttons: Sequence[tuple[str, str]],
        on_action: Callable[[str], None],
        expires_in: timedelta | None = None,
    ) -> None: ...

    def enabled(self) -> bool | None: ...


class TrayMessages(Protocol):
    def show_message(
        self, title: str, text: str, level: StatusLevel = ..., ms: int = ...
    ) -> None: ...

    def notify_alert(self, alert: Alert) -> None: ...


class NotificationCenter(QObject):
    openRequested = Signal()
    switchRequested = Signal(str)  # router id, from a notification's Switch button
    statusChanged = Signal(object)  # True, False, or None when unknown
    _clicked = Signal(str)  # emitted on WinRT threads, handled on the UI thread

    def __init__(
        self,
        controller: AppController,
        tray: TrayMessages,
        toaster: Toaster | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.tray = tray
        self.toaster = toaster
        self._clicked.connect(self._on_clicked)
        controller.alertRaised.connect(self.show_alert)
        controller.testAllFinished.connect(lambda message, _scheduled: self.show_run(message))
        controller.switchFinished.connect(self.show_run)
        controller.autoSwitched.connect(self.show_auto_switch)

    def show_alert(self, alert: Alert) -> None:
        buttons = [("Open", OPEN)]
        if alert.kind is AlertKind.UNSTABLE:
            buttons.append(("Check now", CHECK))
            better = alert.recommended_id
            if better is not None and self.controller.can_switch_to(better):
                buttons.append((f"Switch to {alert.recommended_name}", SWITCH + better))
        self._show(
            f"alert-{alert.router_id}",  # "back to normal" replaces the "unstable" toast
            alert.title,
            alert.text,
            buttons,
            ALERT_LIFETIME,
            lambda: self.tray.notify_alert(alert),
        )

    def show_run(self, message: Message) -> None:
        """Only a connection that couldn't be restored needs a notification."""
        if not message.restore_failed or not self.controller.alerts_allowed(self.controller.now()):
            return
        self._show(
            "reconnect", message.title, message.text, [("Open", OPEN)], ALERT_LIFETIME,
            lambda: self.tray.show_message(message.title, message.text, StatusLevel.BAD, 10_000),
        )  # fmt: skip

    def show_auto_switch(self, message: Message) -> None:
        """An automatic switch (or going back) happened; offer to undo a switch."""
        if not self.controller.alerts_allowed(self.controller.now()):
            return
        buttons = [("Open", OPEN)]
        if message.back_to is not None:
            buttons.append(("Go back", SWITCH + message.back_to))
        level = StatusLevel.GOOD if message.level is StatusLevel.GOOD else StatusLevel.BAD
        self._show(
            "auto-switch", message.title, message.text, buttons, ALERT_LIFETIME,
            lambda: self.tray.show_message(message.title, message.text, level, 10_000),
        )  # fmt: skip

    def notify_hidden(self) -> None:
        title = "Router Checker is still running"
        body = "It keeps checking in the background. Use the tray icon to open it or to exit."
        self._show(
            "hint", title, body, [("Open", OPEN)], HINT_LIFETIME,
            lambda: self.tray.show_message(title, body),
        )  # fmt: skip

    def send_test(self) -> None:
        title = "Notifications work"
        body = "This is how Router Checker tells you that your router is unstable."
        self._show(
            "test", title, body, [("Open", OPEN)], HINT_LIFETIME,
            lambda: self.tray.show_message(title, body, StatusLevel.GOOD),
        )  # fmt: skip
        self.refresh_status()

    def refresh_status(self) -> None:
        """Tell listeners whether Windows lets this app show notifications."""
        self.statusChanged.emit(self.toaster.enabled() if self.toaster is not None else None)

    def _show(
        self,
        tag: str,
        title: str,
        body: str,
        buttons: Sequence[tuple[str, str]],
        lifetime: timedelta,
        fallback: Callable[[], None],
    ) -> None:
        if self.toaster is not None:
            try:
                self.toaster.show(
                    tag=tag,
                    title=title,
                    body=body,
                    buttons=buttons,
                    on_action=self._clicked.emit,
                    expires_in=lifetime,
                )
                return
            except Exception:
                log.warning("Windows notification failed; using the tray instead", exc_info=True)
        fallback()

    def _on_clicked(self, action: str) -> None:
        if action == CHECK:
            self.controller.check_now()
        elif action.startswith(SWITCH):
            self.switchRequested.emit(action.removeprefix(SWITCH))
        self.openRequested.emit()  # every click opens the window, so progress shows
