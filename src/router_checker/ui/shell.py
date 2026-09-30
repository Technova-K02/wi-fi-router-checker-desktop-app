"""Open Windows Settings pages and folders (no blocking, no admin rights)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

LOCATION_SETTINGS = "ms-settings:privacy-location"
NOTIFICATION_SETTINGS = "ms-settings:notifications"


def open_location_settings() -> None:
    QDesktopServices.openUrl(QUrl(LOCATION_SETTINGS))


def open_notification_settings() -> None:
    QDesktopServices.openUrl(QUrl(NOTIFICATION_SETTINGS))


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
