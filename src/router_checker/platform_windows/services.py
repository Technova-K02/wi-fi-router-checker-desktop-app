"""Small platform services: DNS timing, the clock and the data folder."""

from __future__ import annotations

import os
import socket
import time
from datetime import UTC, datetime
from pathlib import Path

from router_checker.core.models import DnsResult

APP_DIR_NAME = "RouterChecker"


def data_dir() -> Path:
    """%LOCALAPPDATA%\\RouterChecker."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_DIR_NAME


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class SocketDnsService:
    """Times socket.getaddrinfo. The Windows DNS cache answers repeat lookups,
    so this measures what apps actually wait for, not a cold lookup."""

    def resolve(self, host: str) -> DnsResult:
        start = time.perf_counter()
        try:
            infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except OSError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return DnsResult(host, (), elapsed, error=str(exc))
        elapsed = (time.perf_counter() - start) * 1000
        addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
        return DnsResult(host, addresses, elapsed)


def open_location_settings() -> None:
    os.startfile("ms-settings:privacy-location")  # type: ignore[attr-defined]


def set_app_user_model_id(app_id: str) -> None:
    """Give the process its own taskbar identity (and later, toast identity)."""
    import ctypes

    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(app_id))
