"""Windows notifications with buttons ("toasts") through the windows-toasts package.

Toasts show the app's own name and icon once its app id is registered under
``HKCU\\Software\\Classes\\AppUserModelId`` (no admin rights needed; the only
thing the app writes outside its data folder). Clicks on a toast or one of
its buttons arrive on a WinRT thread: ``on_action`` must hand them over to the
UI thread (the UI does that with a Qt signal).
"""

from __future__ import annotations

import logging
import winreg
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from windows_toasts import InteractableWindowsToaster, Toast, ToastActivatedEventArgs, ToastButton
from winrt.windows.ui.notifications import NotificationSetting

log = logging.getLogger(__name__)

APP_IDS_KEY = r"Software\Classes\AppUserModelId"
GROUP = "router-checker"
KEEP_ALIVE = 10  # recent toasts whose clicks must still arrive


def register_app_id(app_id: str, display_name: str, icon: Path | None = None) -> None:
    """Create or update ``HKCU\\Software\\Classes\\AppUserModelId\\<app_id>``."""
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER, rf"{APP_IDS_KEY}\{app_id}", 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
        if icon is not None:
            winreg.SetValueEx(key, "IconUri", 0, winreg.REG_SZ, str(icon))


class _Recorder:
    """Wraps the WinRT ToastNotifier to keep the notifications it shows."""

    def __init__(self, notifier: Any) -> None:
        self._notifier = notifier
        self.last: Any = None

    def show(self, notification: Any) -> None:
        self.last = notification
        self._notifier.show(notification)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._notifier, name)


class WindowsToastNotifier:
    def __init__(self, app_id: str, app_name: str) -> None:
        self._toaster = InteractableWindowsToaster(app_name, notifierAUMID=app_id)
        self._recorder = _Recorder(self._toaster.toastNotifier)
        self._toaster.toastNotifier = self._recorder
        # A toast's click handlers live on its WinRT object; keep the newest ones alive.
        self._alive: OrderedDict[str, tuple[Toast, Any]] = OrderedDict()

    def show(
        self,
        *,
        tag: str,
        title: str,
        body: str,
        buttons: Sequence[tuple[str, str]],
        on_action: Callable[[str], None],
        expires_in: timedelta | None = None,
    ) -> None:
        """Show a toast; one with the same ``tag`` replaces the earlier one.

        ``buttons`` are (label, action) pairs. ``on_action`` gets the action of the
        clicked button, or the tag when the toast itself is clicked.
        """

        def activated(args: ToastActivatedEventArgs) -> None:
            try:
                on_action(args.arguments or tag)
            except Exception:
                log.exception("handling a notification click failed")

        toast = Toast(
            text_fields=[title, body],
            actions=[ToastButton(label, arguments=action) for label, action in buttons],
            group=GROUP,
            expiration_time=datetime.now(UTC) + expires_in if expires_in else None,
            on_activated=activated,
            on_failed=lambda args: log.warning("a notification failed: %s", args.error_code),
        )
        toast.tag = tag
        self._toaster.show_toast(toast)
        self._alive[tag] = (toast, self._recorder.last)
        self._alive.move_to_end(tag)
        while len(self._alive) > KEEP_ALIVE:
            self._alive.popitem(last=False)

    def enabled(self) -> bool | None:
        """False when Windows blocks this app's notifications. None until Windows
        knows the app, which happens with its first toast."""
        try:
            return self._toaster.toastNotifier.setting == NotificationSetting.ENABLED
        except OSError:
            return None
