"""Start with Windows through ``HKCU\\...\\CurrentVersion\\Run`` (no admin rights).

The installer's "Start when I sign in" option writes the same value, and the
uninstaller removes it.
"""

from __future__ import annotations

import sys
import winreg
from pathlib import Path

from router_checker.core.startup import RUN_VALUE, approved, startup_command

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def app_program() -> list[str]:
    """How to start this copy of the app: the .exe when built, otherwise
    pythonw (no console window) with the UI module."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else sys.executable), "-m", "router_checker.ui"]


def is_built() -> bool:
    return bool(getattr(sys, "frozen", False))


class WindowsStartup:
    def __init__(
        self,
        command: str | None = None,
        *,
        name: str = RUN_VALUE,
        run_key: str = RUN_KEY,
        approved_key: str = APPROVED_KEY,
    ) -> None:
        self.command = command or startup_command(app_program())
        self._name, self._run_key, self._approved_key = name, run_key, approved_key

    def registered(self) -> str | None:
        """The command Windows runs at sign-in, or None."""
        return self._read(self._run_key)

    def enabled(self) -> bool:
        if self.registered() is None:
            return False
        flag = self._read(self._approved_key)
        return approved(flag if isinstance(flag, bytes) else None)

    def set_enabled(self, on: bool) -> None:
        # Turning it on here also undoes "Disabled" from Task Manager's Startup apps.
        self._delete(self._approved_key)
        if on:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER, self._run_key, 0, winreg.KEY_SET_VALUE
            ) as key:
                winreg.SetValueEx(key, self._name, 0, winreg.REG_SZ, self.command)
        else:
            self._delete(self._run_key)

    def update_command(self) -> bool:
        """Point an existing entry at this copy of the app (it moved or was
        reinstalled elsewhere). Keeps Task Manager's on/off flag. True if changed."""
        current = self.registered()
        if current is None or current == self.command:
            return False
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, self._run_key, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, self._name, 0, winreg.REG_SZ, self.command)
        return True

    def _read(self, key_path: str):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                return winreg.QueryValueEx(key, self._name)[0]
        except FileNotFoundError:
            return None

    def _delete(self, key_path: str) -> None:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, self._name)
        except FileNotFoundError:
            pass
